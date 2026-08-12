"""Idempotent corpus → graph ingest.

Pipeline per file under corpus_dir (skip README.md by default):
  1. read bytes → sha256 hex content_hash
  2. if store.document_hash(path) == content_hash: skip (no extract call)
  3. else: extract_graph_delta → validate → upsert_delta → set_document_hash
  4. after the walk: soft_delete_source for DB docs no longer on disk

Returns an IngestReport so ops (and tests) can see skipped/extracted/deleted.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .store import KnowledgeGraphStore


@dataclass
class IngestReport:
    extracted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)


def file_content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ingest_corpus(
    corpus_dir: str | Path,
    store: KnowledgeGraphStore,
    *,
    extract: Callable[..., Any] | None = None,
    skip_names: frozenset[str] = frozenset({"README.md"}),
) -> IngestReport:
    """Walk markdown files and upsert graph deltas incrementally.

    The same trick as the vector store's ingest, applied to a graph: the content
    hash is the change detector. Unchanged file → skip the (expensive) extract
    call entirely. Paths are recorded relative to corpus_dir so the DB doesn't
    break when the corpus moves.
    """
    from .extract import extract_graph_delta
    from .validate import SchemaError, assert_closed_world_delta

    corpus_dir = Path(corpus_dir)
    if not corpus_dir.is_dir():
        # rglob on a nonexistent path yields nothing without raising — and an
        # empty walk would make the deletion sweep soft-delete the ENTIRE graph.
        # A typo'd or unmounted corpus path must fail loudly, not wipe data.
        raise FileNotFoundError(f"corpus_dir does not exist: {corpus_dir}")
    extract_fn = extract or extract_graph_delta
    report = IngestReport()

    from docsbot.tracing.instrumentation import span_ingest

    on_disk: set[str] = set()
    for path in sorted(corpus_dir.rglob("*.md")):
        if path.name in skip_names:
            continue
        rel = str(path.relative_to(corpus_dir))
        on_disk.add(rel)  # before any fallible work: an unreadable file is
        # still on disk and must not be swept as deleted

        try:
            text = path.read_text(encoding="utf-8")
            content_hash = file_content_hash(text)
            if store.document_hash(rel) == content_hash:
                report.skipped.append(rel)
                continue
            with span_ingest(rel, content_hash):
                delta = extract_fn(text, source_path=rel, content_hash=content_hash)
            assert_closed_world_delta(delta)
        except SchemaError as err:
            # Bad payload: quarantine the file, don't record its hash — so the
            # next run retries instead of silently treating it as ingested.
            report.quarantined.append(f"{rel}: {err}")
            continue
        except Exception as err:  # unreadable file, LLM failure, bad JSON, ...
            # One broken document must not abort the other N; same retry
            # semantics as SchemaError (no hash recorded -> retried next run).
            report.quarantined.append(f"{rel}: {type(err).__name__}: {err}")
            continue
        report.quarantined.extend(delta.quarantined)

        store.upsert_delta(delta)
        store.set_document_hash(rel, content_hash)
        report.extracted.append(rel)

    # Deletion sweep: docs the store knows about that are no longer on disk.
    for stale in store.document_paths():
        if stale not in on_disk:
            store.soft_delete_source(stale)
            report.deleted.append(stale)

    return report
