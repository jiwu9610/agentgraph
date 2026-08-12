"""Durable sessions + agent checkpoints.

SQLite by default: no external infrastructure required. Schema sketch:

  sessions(id PK, user_id, created_at, updated_at, ttl_seconds)
  messages(session_id, idx, role, content)
  checkpoints(session_id PK, payload_json, updated_at)

Checkpoint payload must be enough to resume the tool loop without re-executing
completed tool calls (store tool_results already obtained + next step).

Design note: the checkpoint is stored as one JSON blob rather than normalized
tables. Its shape changes as the agent evolves; a blob keyed by session_id makes
"save" and "load" total functions and turns schema evolution into a dataclass
edit instead of a migration.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    session_id TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    PRIMARY KEY (session_id, idx)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    session_id   TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AgentCheckpoint:
    session_id: str
    question: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    visited_entity_ids: list[str] = field(default_factory=list)
    path: list[dict[str, str]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    working_memory: dict[str, Any] = field(default_factory=dict)
    completed_tool_call_ids: list[str] = field(default_factory=list)
    status: str = "running"  # running | answered | refused | budget_exceeded


class SessionStore:
    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def create_session(self, user_id: str, *, ttl_seconds: int = 86400) -> str:
        sid = uuid.uuid4().hex
        now = _now()
        self._conn.execute(
            "INSERT INTO sessions (id, user_id, created_at, updated_at, ttl_seconds) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, user_id, now, now, ttl_seconds),
        )
        self._conn.commit()
        return sid

    def append_message(self, session_id: str, role: str, content: str) -> None:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(idx), -1) + 1 AS next FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        self._conn.execute(
            "INSERT INTO messages (session_id, idx, role, content) VALUES (?, ?, ?, ?)",
            (session_id, row["next"], role, content),
        )
        self._conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id)
        )
        self._conn.commit()

    def history(self, session_id: str) -> list[dict[str, str]]:
        rows = self._conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY idx",
            (session_id,),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    def save_checkpoint(self, checkpoint: AgentCheckpoint) -> None:
        self._conn.execute(
            """
            INSERT INTO checkpoints (session_id, payload_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (checkpoint.session_id, json.dumps(asdict(checkpoint)), _now()),
        )
        self._conn.commit()

    def load_checkpoint(self, session_id: str) -> AgentCheckpoint | None:
        row = self._conn.execute(
            "SELECT payload_json FROM checkpoints WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        # Tolerate blobs written by a different checkpoint schema: unknown keys
        # are dropped rather than raising TypeError at resume time.
        known = {f.name for f in dataclasses.fields(AgentCheckpoint)}
        return AgentCheckpoint(**{k: v for k, v in payload.items() if k in known})

    def compact_working_memory(self, session_id: str, *, keep_last_tools: int = 3) -> None:
        """Drop bulky tool payloads; keep entity ids + path.

        Tool results are the bulk (raw neighbor lists etc.); the distilled state
        the agent actually needs across turns — what it visited and the path it
        can cite — is tiny and is always retained.
        """
        cp = self.load_checkpoint(session_id)
        if cp is None:
            return
        cp.tool_results = cp.tool_results[-keep_last_tools:] if keep_last_tools > 0 else []
        # cp.messages duplicates every tool payload verbatim — dropping tool
        # messages whose results were compacted is what actually shrinks the blob.
        kept_ids = {r.get("id") for r in cp.tool_results}
        cp.messages = [
            m for m in cp.messages
            if m.get("role") != "tool" or m.get("id") in kept_ids
        ]
        self.save_checkpoint(cp)

    def delete_session(self, session_id: str) -> None:
        self._conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        self._conn.execute("DELETE FROM checkpoints WHERE session_id = ?", (session_id,))
        self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
