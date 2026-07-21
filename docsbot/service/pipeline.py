"""The DocsBot service: retrieve → rerank → answer.

Every change here is annotated with the incident it closes, because in a real
codebase the *reason* for an optimization is the part that stops someone from
undoing it six months later.

Incident-by-incident analysis: PART4_POSTMORTEMS.md.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..config import settings
from ..ingest import Chunk, chunk_text
from ..perf.harness import MAX_BATCH, FakeProvider
from .cache import AnswerCache
from .sessions import SessionStore


@dataclass
class Citation:
    source: str
    index: int
    score: float


@dataclass
class Answer:
    text: str
    citations: list[Citation] = field(default_factory=list)
    latency_s: float = 0.0
    cached: bool = False


class _NullMetrics:
    def span(self, name: str):
        from contextlib import nullcontext
        return nullcontext()

    def count(self, name: str, n: int = 1) -> None:
        pass

    def observe(self, name: str, value: float) -> None:
        pass


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_corpus() -> dict[str, str]:
    """The corpus: the Northstar Cloud engineering handbook.

    Production-sized on purpose. A three-file toy corpus hides exactly the
    problems this service is built around — when every document fits in a
    single chunk, "send the whole document instead of the chunk" costs nothing.
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    corpus_dir = root / "corpus" / "handbook"
    if not corpus_dir.exists():
        corpus_dir = root / "docs"
    return {p.name: p.read_text(encoding="utf-8")
            for p in sorted(corpus_dir.glob("*.md"))}


class DocsService:
    """Retrieve → rerank → answer, with a cache and multi-turn sessions."""

    def __init__(self, provider: FakeProvider, *, metrics=None) -> None:
        self.provider = provider
        self.metrics = metrics or _NullMetrics()

        self._docs: dict[str, str] = {}
        self._chunks: list[Chunk] = []
        self._vectors: list[list[float]] = []
        # INC-002: content hash -> embedding, so unchanged text is never re-embedded.
        self._vector_by_hash: dict[str, list[float]] = {}
        self._indexed = False

        self.cache = AnswerCache(ttl_s=settings.answer_cache_ttl_s)
        self.sessions = SessionStore(ttl_s=settings.session_ttl_s,
                                     max_sessions=settings.max_sessions)

    # ------------------------------------------------------------------
    # indexing
    # ------------------------------------------------------------------
    def index(self, docs: dict[str, str]) -> int:
        """Chunk and embed a corpus, incrementally and in batches.

        INC-002 fixed two things here:
          * Unchanged chunks reuse their cached embedding (content hash), so a
            deploy that touches no docs costs zero provider calls.
          * New chunks are embedded in BATCHES. The embedding endpoint charges
            one round-trip per call, not per text — 50 chunks one-at-a-time is
            50 round-trips; one batch of 50 is one.
        """
        with self.metrics.span("index"):
            self._docs.update(docs)

            chunks: list[Chunk] = []
            for source, text in sorted(docs.items()):
                chunks.extend(chunk_text(text, source=source))

            hashes = [_content_hash(c.text) for c in chunks]
            todo = [(h, c) for h, c in zip(hashes, chunks)
                    if h not in self._vector_by_hash]

            # Deduplicate within this batch too: identical text embeds once.
            unique: dict[str, str] = {}
            for h, c in todo:
                unique.setdefault(h, c.text)

            pending = list(unique.items())
            for start in range(0, len(pending), MAX_BATCH):
                page = pending[start:start + MAX_BATCH]
                vectors = self.provider.embed([text for _, text in page],
                                              task_type="RETRIEVAL_DOCUMENT",
                                              tag="index")
                for (h, _), vec in zip(page, vectors):
                    self._vector_by_hash[h] = vec
                self.metrics.count("chunks_embedded", len(page))

            self._chunks = chunks
            self._vectors = [self._vector_by_hash[h] for h in hashes]
            self._indexed = True
            return len(self._chunks)

    def _ensure_indexed(self) -> None:
        if not self._indexed:
            self.index(load_corpus())

    # ------------------------------------------------------------------
    # liveness
    # ------------------------------------------------------------------
    def health(self) -> dict:
        """Liveness probe: cheap, local, and free.

        INC-001: this used to lazily index the corpus, so the first probe after
        a deploy triggered a full embed inside the load balancer's timeout —
        the probe timed out, the LB pulled the instance, and the replacement
        did exactly the same thing. A health check must never be the thing that
        does the expensive work.
        """
        return {"status": "ok", "chunks": len(self._chunks),
                "indexed": self._indexed}

    # ------------------------------------------------------------------
    # retrieval
    # ------------------------------------------------------------------
    def _vector_search(self, qvec: list[float], limit: int) -> list[tuple[int, float]]:
        scored = [(i, sum(a * b for a, b in zip(qvec, vec)))
                  for i, vec in enumerate(self._vectors)]
        scored.sort(key=lambda p: p[1], reverse=True)
        return scored[:limit]

    def _keyword_search(self, question: str, limit: int) -> list[tuple[int, float]]:
        q_terms = [w.strip(".,!?;:()").lower()
                   for w in question.split() if len(w) > 3]
        if not q_terms:
            return []
        n_docs = len(self._chunks) or 1
        df = Counter()
        tokenized = []
        for chunk in self._chunks:
            terms = [w.strip(".,!?;:()").lower() for w in chunk.text.split()]
            tokenized.append(Counter(terms))
            for t in set(terms):
                df[t] += 1
        scored = []
        for i, tf in enumerate(tokenized):
            score = 0.0
            for term in q_terms:
                if tf.get(term):
                    idf = math.log(1 + n_docs / (1 + df.get(term, 0)))
                    score += tf[term] * idf
            scored.append((i, score))
        scored.sort(key=lambda p: p[1], reverse=True)
        return scored[:limit]

    def _fuse(self, vector_hits, keyword_hits) -> list[int]:
        k = settings.rrf_k
        fused: dict[int, float] = {}
        for rank, (idx, _) in enumerate(vector_hits):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (k + rank + 1)
        for rank, (idx, _) in enumerate(keyword_hits):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (k + rank + 1)
        return [idx for idx, _ in sorted(fused.items(), key=lambda p: p[1],
                                         reverse=True)]

    def _rerank(self, question: str, candidate_ids: list[int]) -> list[tuple[int, float]]:
        """Model-scored relevance for every candidate — in ONE call.

        INC-001: this used to make one call per candidate, serially. Eight
        candidates meant eight round-trips stacked end to end, and reranking
        alone was ~80% of p95.

        Batching beats parallelising here. Running the eight calls concurrently
        would hide the latency but still cost eight calls; one batched call
        costs one. Fewer calls is strictly better than faster calls — it helps
        latency AND cost AND your rate limit at the same time.
        """
        if not candidate_ids:
            return []

        with self.metrics.span("rerank"):
            passages = [self._chunks[i].text for i in candidate_ids]
            reply = self.provider.chat(
                "SCOREMANY:" + question + "|||" + "|||".join(passages),
                model=settings.chat_model,
                tag="rerank",
            )
            scores: list[float] = []
            for raw in reply.split(","):
                try:
                    scores.append(float(raw.strip()))
                except ValueError:
                    scores.append(0.0)
            scores += [0.0] * (len(candidate_ids) - len(scores))

            scored = list(zip(candidate_ids, scores))
            scored.sort(key=lambda p: p[1], reverse=True)
            return scored

    # ------------------------------------------------------------------
    # prompting
    # ------------------------------------------------------------------
    def _build_prompt(self, question: str, ranked: list[tuple[int, float]],
                      history: list[tuple[str, str]] | None = None) -> str:
        """Assemble the grounded prompt.

        INC-002 fixed two token sinks:
          * We send the retrieved CHUNK, not the whole source document. The
            chunk is what retrieval actually judged relevant; the rest of the
            file is padding you pay for on every single request.
          * We send the top_k chunks, not every candidate the reranker scored.
            Scoring 8 and grounding on the best 4 is the point of reranking.
        """
        parts: list[str] = []

        # INC-004: only the last N turns. Resending the whole transcript makes
        # cost grow quadratically with conversation length and eventually
        # overruns the context window outright.
        if history:
            for user_msg, assistant_msg in history[-settings.history_window_turns:]:
                parts.append(f"User: {user_msg}\nAssistant: {assistant_msg}")

        for idx, _score in ranked[:settings.top_k]:
            chunk = self._chunks[idx]
            parts.append(f"[source: {chunk.source} #{chunk.index}]\n{chunk.text}")

        parts.append(f"Question: {question}")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # the request path
    # ------------------------------------------------------------------
    def ask(self, question: str, *, history: list[tuple[str, str]] | None = None) -> Answer:
        started = time.perf_counter()

        # INC-001: no global lock. The provider client is thread-safe, and
        # serialising every request behind one mutex meant eight concurrent
        # users waited eight times as long as one user.
        with self.metrics.span("ask"):
            self._ensure_indexed()

            # INC-002: check the cache FIRST. It used to be consulted after
            # classification, retrieval, and reranking had already run, so a
            # "hit" still cost nine provider calls — a cache that saves the
            # cheapest part of the request and pays for the rest.
            cache_key = AnswerCache.key(question, model=settings.chat_model,
                                        top_k=settings.top_k)
            if not history:
                cached = self.cache.get(cache_key)
                if cached is not None:
                    self.metrics.count("cache_hit")
                    return Answer(text=cached.text, citations=cached.citations,
                                  latency_s=time.perf_counter() - started,
                                  cached=True)
            self.metrics.count("cache_miss")

            # INC-002: a yes/no guard does not need the flagship model.
            with self.metrics.span("classify"):
                self.provider.chat(f"CLASSIFY:{question}",
                                   model=settings.cheap_model, tag="classify")

            with self.metrics.span("retrieve"):
                # INC-001: embed the query ONCE and reuse the vector. It used to
                # be embedded separately for vector search and for fusion.
                qvec = self.provider.embed([question], task_type="RETRIEVAL_QUERY",
                                           tag="query_embed")[0]
                vector_hits = self._vector_search(qvec, settings.rerank_top_n * 2)
                keyword_hits = self._keyword_search(question, settings.rerank_top_n * 2)
                fused = self._fuse(vector_hits, keyword_hits)

            ranked = self._rerank(question, fused[:settings.rerank_top_n])

            prompt = self._build_prompt(question, ranked, history=history)
            with self.metrics.span("generate"):
                text = self.provider.chat(prompt, system=_RAG_SYSTEM,
                                          model=settings.chat_model, tag="answer")

            citations = [
                Citation(source=self._chunks[i].source,
                         index=self._chunks[i].index, score=s)
                for i, s in ranked[:settings.top_k]
            ]
            answer = Answer(text=text, citations=citations,
                            latency_s=time.perf_counter() - started)
            if not history:
                self.cache.put(cache_key, answer)
            return answer

    def chat(self, session_id: str, message: str) -> Answer:
        history = self.sessions.history(session_id)
        answer = self.ask(message, history=history)
        self.sessions.append(session_id, message, answer.text)
        return answer


_RAG_SYSTEM = (
    "You are DocsBot. Answer using ONLY the provided context. If the answer is "
    "not in the context, say 'I couldn't find that in the docs.' Cite the "
    "source filename(s) you used in parentheses."
)
