"""Disk-backed vector store built on Chroma, with incremental indexing.

Drop-in replacement for :class:`docsbot.store.VectorStore`: it exposes the same
``add`` / ``search`` / ``__len__`` interface, but the index is persisted to disk
(so it survives restarts) and re-indexing is incremental — chunks whose content
is already in the collection are not re-embedded.

Incrementality works by keying every chunk on a stable content-derived id
(see :func:`chunk_id`). If a chunk's source, index, or text changes, its id
changes, so it is treated as new and re-embedded; unchanged chunks are skipped.

Score convention: the collection uses cosine distance, and ``Hit.score`` is
reported as ``1 - distance`` — i.e. cosine similarity, higher is better — the
same convention as the in-memory store.
"""

from __future__ import annotations

import hashlib

import chromadb

from .config import settings
from .ingest import Chunk
from .store import Hit, _embed  # shared embedder + result dataclasses


def chunk_id(chunk: Chunk) -> str:
    """Stable, unique id for a chunk, used as its primary key in the DB.

    Deterministic across runs, and sensitive to every field that defines the
    chunk's identity (source, index, and text) — so an edited chunk gets a new
    id and is re-embedded, while an unchanged one is skipped. Fields are joined
    with a NUL separator before hashing so no two field combinations collide.
    """
    return _content_hash(f"{chunk.source}\x00{chunk.index}\x00{chunk.text}")


class PersistentVectorStore:
    """A disk-backed vector store with incremental indexing.

    Same interface as the in-memory store (``add`` / ``search`` / ``__len__``),
    but backed by a persistent Chroma collection so embeddings are computed at
    most once per unique chunk and survive process restarts.
    """

    def __init__(self, db_path: str | None = None, collection: str | None = None) -> None:
        """Open (or create) the on-disk collection.

        Defaults come from settings (``db_path`` / ``collection_name``); both
        can be overridden, e.g. to point tests at a temp directory. The
        collection is created with cosine distance so scores convert directly
        to cosine similarity.
        """
        client = chromadb.PersistentClient(path=db_path or settings.db_path)
        self._collection = client.get_or_create_collection(
            name=collection or settings.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def add(self, chunks: list[Chunk]) -> None:
        """Embed and upsert chunks, skipping any already indexed unchanged.

        A chunk's id encodes its full content, so "already present in the
        collection" means "identical content already embedded" — those chunks
        cost nothing on re-index. Adding the same chunks twice neither
        re-embeds nor duplicates them.
        """
        if not chunks:
            return

        # Deduplicate within the batch while preserving order; duplicate ids in
        # a single upsert are an error in Chroma.
        by_id: dict[str, Chunk] = {}
        for c in chunks:
            by_id.setdefault(chunk_id(c), c)

        existing = set(self._collection.get(ids=list(by_id))["ids"])
        to_index = [(cid, c) for cid, c in by_id.items() if cid not in existing]
        if not to_index:
            return  # everything unchanged — no embedding calls at all

        vectors = _embed([c.text for _, c in to_index], task_type="RETRIEVAL_DOCUMENT")
        self._collection.upsert(
            ids=[cid for cid, _ in to_index],
            embeddings=vectors.tolist(),
            documents=[c.text for _, c in to_index],
            metadatas=[{"source": c.source, "index": c.index} for _, c in to_index],
        )

    def search(self, query: str, k: int | None = None) -> list[Hit]:
        """Return the k most similar chunks as Hits, highest score first.

        The query is embedded with the query-side task type, and Chroma's
        cosine distances are converted to similarities (``1 - distance``) so
        higher means better. ``k`` defaults to ``settings.top_k`` and is capped
        at the collection size.
        """
        k = k or settings.top_k
        count = self._collection.count()
        if count == 0:
            return []

        q = _embed([query], task_type="RETRIEVAL_QUERY")
        res = self._collection.query(
            query_embeddings=q.tolist(),
            n_results=min(k, count),
            include=["documents", "metadatas", "distances"],
        )

        hits: list[Hit] = []
        for text, meta, distance in zip(
            res["documents"][0], res["metadatas"][0], res["distances"][0]
        ):
            chunk = Chunk(text=text, source=meta["source"], index=int(meta["index"]))
            hits.append(Hit(chunk=chunk, score=1.0 - float(distance)))
        # Chroma returns rows ordered by ascending distance, i.e. descending
        # similarity — already the order we promise.
        return hits

    def __len__(self) -> int:
        """Number of chunks currently persisted."""
        return self._collection.count()


def _content_hash(text: str) -> str:
    """Short stable hex digest of ``text``; stable across runs and platforms."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
