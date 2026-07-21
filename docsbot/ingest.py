"""Turn documents into chunks.

RAG has two halves: a data side (this file + store.py) and a query side (rag.py).
This is the data side, and it has NO AI in it at all — it's plain text wrangling.
Half of "AI engineering" is unglamorous data plumbing.

Why chunk? Two reasons:
  1. Embedding models have an input limit (gemini-embedding-001: ~2048 tokens).
  2. Retrieval is sharper on small passages than whole documents — you want to
     pull the one relevant paragraph, not a 50-page manual.

We overlap chunks so a sentence split across a boundary still lands whole in at
least one chunk:

    [....chunk 1....]
              [....chunk 2....]      <- overlap region shared by both
                        [....chunk 3....]
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import settings


@dataclass
class Chunk:
    """A slice of a source document, plus where it came from (for citations)."""

    text: str
    source: str       # filename it came from
    index: int        # which chunk number within that file


def chunk_text(text: str, source: str) -> list[Chunk]:
    """Split one document into overlapping character windows."""
    size = settings.chunk_size
    overlap = settings.chunk_overlap
    step = size - overlap
    if step <= 0:
        raise ValueError("chunk_size must be larger than chunk_overlap")

    chunks: list[Chunk] = []
    start = 0
    i = 0
    while start < len(text):
        piece = text[start : start + size].strip()
        if piece:
            chunks.append(Chunk(text=piece, source=source, index=i))
            i += 1
        start += step
    return chunks


def load_corpus(docs_dir: str | Path) -> list[Chunk]:
    """Read every .md/.txt file under docs_dir and chunk them all."""
    docs_dir = Path(docs_dir)
    chunks: list[Chunk] = []
    for path in sorted(docs_dir.glob("**/*")):
        if path.suffix.lower() not in {".md", ".txt"}:
            continue
        chunks.extend(chunk_text(path.read_text(encoding="utf-8"), source=path.name))
    return chunks
