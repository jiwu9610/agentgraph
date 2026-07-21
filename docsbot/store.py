"""Embeddings + a vector store you can actually read.

This is where "semantic search" stops being magic. An embedding is just a list
of numbers (a vector) that captures meaning: texts about similar things land
near each other. "Closeness" = cosine similarity = the angle between vectors.

The store is deliberately HAND-ROLLED as a list + numpy: ~15 lines with every
moving part visible — the core really is that simple. `store_persistent.py`
swaps in a real vector DB (Chroma) behind the same interface.

    embed each chunk ──▶ matrix of vectors (N x 768)
    embed the query  ──▶ one vector (768,)
    cosine(query, every chunk) ──▶ scores ──▶ take the top-k highest

Gemini detail worth knowing: embeddings are TASK-TYPED. Documents are embedded
with RETRIEVAL_DOCUMENT and the question with RETRIEVAL_QUERY. Using the right
task type on each side measurably improves retrieval.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from google.genai import types

from .client import get_client
from .config import settings
from .ingest import Chunk


def _embed(texts: list[str], task_type: str) -> np.ndarray:
    """Embed a list of texts into an (len(texts) x embed_dim) float array."""
    response = get_client().models.embed_content(
        model=settings.embed_model,
        contents=texts,
        config=types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=settings.embed_dim,
        ),
    )
    return np.array([e.values for e in response.embeddings], dtype=np.float32)


@dataclass
class Hit:
    """A retrieved chunk and how well it matched the query."""

    chunk: Chunk
    score: float


class VectorStore:
    """An in-memory store: parallel lists of chunks and their vectors."""

    def __init__(self) -> None:
        self._chunks: list[Chunk] = []
        self._vectors: np.ndarray | None = None  # shape (N, embed_dim)

    def add(self, chunks: list[Chunk]) -> None:
        """Embed chunks (as documents) and keep them for searching."""
        if not chunks:
            return
        vectors = _embed([c.text for c in chunks], task_type="RETRIEVAL_DOCUMENT")
        self._chunks.extend(chunks)
        self._vectors = (
            vectors if self._vectors is None else np.vstack([self._vectors, vectors])
        )

    def search(self, query: str, k: int | None = None) -> list[Hit]:
        """Return the k chunks most semantically similar to the query."""
        if self._vectors is None:
            return []
        k = k or settings.top_k
        q = _embed([query], task_type="RETRIEVAL_QUERY")[0]

        # Cosine similarity = normalized dot product. Normalizing here also makes
        # this correct regardless of whether the embeddings came back normalized.
        doc_norms = self._vectors / np.linalg.norm(self._vectors, axis=1, keepdims=True)
        q_norm = q / np.linalg.norm(q)
        scores = doc_norms @ q_norm  # shape (N,)

        top = np.argsort(scores)[::-1][:k]
        return [Hit(chunk=self._chunks[i], score=float(scores[i])) for i in top]

    def __len__(self) -> int:
        return len(self._chunks)
