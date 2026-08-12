"""SQLite-backed knowledge graph store.

Design notes
------------
- Three tables: entities, triples, documents.
- ``UNIQUE(subject_id, predicate, object_id, source_path)`` makes re-ingesting
  the same file idempotent, while the *same fact from a different file* gets its
  own row — that is multi-provenance, and it is what lets an answer cite every
  document that asserts a fact.
- Entities carry a ``norm_name`` column so ``lookup("mira chen")`` finds
  "Mira Chen" without a table scan of Python-side casefolds.
- WAL journal mode so agent turns (readers) don't block ingest (writer).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .schema import Entity, GraphDelta, Triple, Provenance, normalize_name

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id         TEXT PRIMARY KEY,
    type       TEXT NOT NULL,
    name       TEXT NOT NULL,
    norm_name  TEXT NOT NULL,
    attrs_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_norm ON entities(norm_name);

CREATE TABLE IF NOT EXISTS triples (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id   TEXT NOT NULL,
    predicate    TEXT NOT NULL,
    object_id    TEXT NOT NULL,
    source_path  TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    extracted_at TEXT NOT NULL,
    UNIQUE (subject_id, predicate, object_id, source_path)
);
CREATE INDEX IF NOT EXISTS idx_triples_subject ON triples(subject_id);
CREATE INDEX IF NOT EXISTS idx_triples_object  ON triples(object_id);
CREATE INDEX IF NOT EXISTS idx_triples_source  ON triples(source_path);

CREATE TABLE IF NOT EXISTS documents (
    path              TEXT PRIMARY KEY,
    content_hash      TEXT NOT NULL,
    last_ingested_at  TEXT NOT NULL
);
"""


def _row_to_entity(row: sqlite3.Row) -> Entity:
    return Entity(
        type=row["type"],
        name=row["name"],
        attrs=json.loads(row["attrs_json"]),
    )


def _row_to_triple(row: sqlite3.Row) -> Triple:
    return Triple(
        subject_id=row["subject_id"],
        predicate=row["predicate"],
        object_id=row["object_id"],
        provenance=Provenance(
            source_path=row["source_path"],
            content_hash=row["content_hash"],
            extracted_at=datetime.fromisoformat(row["extracted_at"]),
        ),
    )


class KnowledgeGraphStore:
    """Durable graph. Safe to open from multiple agent turns (WAL recommended)."""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def upsert_delta(self, delta: GraphDelta) -> None:
        """Insert/update entities and triples from one extraction.

        Atomic: the connection context manager commits the whole delta or rolls
        it back — an exception mid-loop can't leave half a document's facts as
        orphan rows for the next unrelated commit to persist.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._upsert_rows(delta, now)

    def _upsert_rows(self, delta: GraphDelta, now: str) -> None:
        for e in delta.entities:
            self._conn.execute(
                """
                INSERT INTO entities (id, type, name, norm_name, attrs_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    attrs_json = excluded.attrs_json,
                    updated_at = excluded.updated_at
                """,
                (e.id, e.type, e.name, normalize_name(e.name), json.dumps(e.attrs), now),
            )
        for t in delta.triples:
            # Same fact + same file → replace (idempotent re-ingest refreshes the
            # hash/timestamp). Same fact + different file → new row (provenance).
            self._conn.execute(
                """
                INSERT INTO triples
                    (subject_id, predicate, object_id, source_path, content_hash, extracted_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(subject_id, predicate, object_id, source_path) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    extracted_at = excluded.extracted_at
                """,
                (
                    t.subject_id,
                    t.predicate,
                    t.object_id,
                    t.provenance.source_path,
                    t.provenance.content_hash,
                    t.provenance.extracted_at.isoformat(),
                ),
            )

    def get_entity(self, entity_id: str) -> Entity | None:
        row = self._conn.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return _row_to_entity(row) if row else None

    def lookup(self, name: str, entity_type: str | None = None) -> list[Entity]:
        """Case-insensitive substring match; optional type filter. May return many.

        Substring (not exact) on purpose: the agent must SEE ambiguity — a query
        for "Mira" should surface both "Mira Chen" and "Mira Chen Bot" so it can
        disambiguate, rather than silently picking one. Exact matches sort first.
        """
        norm = normalize_name(name)
        if not norm:
            return []  # LIKE '%%' would return the whole table
        # Escape LIKE wildcards so a literal % or _ in a name can't widen the match.
        fragment = norm.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where = r"norm_name LIKE '%' || ? || '%' ESCAPE '\'"
        params: list[str] = [fragment]
        if entity_type is not None:
            where += " AND type = ?"
            params.append(entity_type)
        rows = self._conn.execute(
            f"SELECT * FROM entities WHERE {where} "
            "ORDER BY (norm_name = ?) DESC, id",
            (*params, norm),
        ).fetchall()
        return [_row_to_entity(r) for r in rows]

    def neighbors(
        self,
        entity_id: str,
        *,
        predicate: str | None = None,
        direction: str = "out",
    ) -> list[Triple]:
        """direction: 'out' | 'in' | 'both'."""
        if direction == "out":
            where, params = "subject_id = ?", [entity_id]
        elif direction == "in":
            where, params = "object_id = ?", [entity_id]
        elif direction == "both":
            where, params = "(subject_id = ? OR object_id = ?)", [entity_id, entity_id]
        else:
            raise ValueError(f"direction must be out|in|both, got {direction!r}")
        if predicate is not None:
            where += " AND predicate = ?"
            params.append(predicate)
        rows = self._conn.execute(
            f"SELECT * FROM triples WHERE {where} ORDER BY id", params
        ).fetchall()
        return [_row_to_triple(r) for r in rows]

    def follow_edge(self, entity_id: str, predicate: str) -> list[Entity]:
        """Convenience: outbound neighbors via predicate, resolved to Entity."""
        out = self.neighbors(entity_id, predicate=predicate, direction="out")
        seen: set[str] = set()
        entities: list[Entity] = []
        for t in out:
            if t.object_id in seen:
                continue  # same fact from several files → one entity, not three
            seen.add(t.object_id)
            e = self.get_entity(t.object_id)
            if e is not None:
                entities.append(e)
        return entities

    def document_hash(self, source_path: str) -> str | None:
        """Last ingested content hash for path, or None if never seen."""
        row = self._conn.execute(
            "SELECT content_hash FROM documents WHERE path = ?", (source_path,)
        ).fetchone()
        return row["content_hash"] if row else None

    def set_document_hash(self, source_path: str, content_hash: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO documents (path, content_hash, last_ingested_at)
            VALUES (?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                content_hash = excluded.content_hash,
                last_ingested_at = excluded.last_ingested_at
            """,
            (source_path, content_hash, now),
        )
        self._conn.commit()

    def document_paths(self) -> list[str]:
        """Every path the store has ingested — the deletion sweep compares this
        against what is still on disk."""
        rows = self._conn.execute("SELECT path FROM documents ORDER BY path").fetchall()
        return [r["path"] for r in rows]

    def soft_delete_source(self, source_path: str) -> int:
        """Remove triples (and the document row) for a path.

        Entities are left in place: they may be referenced by triples from other
        sources, and a dangling entity is harmless while a dangling edge is not.
        Returns number of triples removed.
        """
        cur = self._conn.execute(
            "DELETE FROM triples WHERE source_path = ?", (source_path,)
        )
        removed = cur.rowcount
        # Drop the doc row too: if the file ever comes back, it must re-extract
        # rather than being skipped by a stale matching hash.
        self._conn.execute("DELETE FROM documents WHERE path = ?", (source_path,))
        self._conn.commit()
        return removed

    def all_triples(self) -> list[Triple]:
        rows = self._conn.execute("SELECT * FROM triples ORDER BY id").fetchall()
        return [_row_to_triple(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
