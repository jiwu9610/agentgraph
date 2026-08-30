"""Hybrid retrieval: BM25 keyword ranking, rank fusion, and LLM reranking.

Vector search matches by meaning but can miss exact terms (error codes, rare
identifiers); keyword search is the reverse. Hybrid retrieval runs both, fuses
the two rankings, and optionally reranks the fused candidates with a model:

    query --> BM25 ranking ------\
          --> vector ranking ----+--> reciprocal-rank fusion --> rerank --> top_k

The three pieces are independent:

- ``BM25`` implements the standard Okapi BM25 formula (k1=1.5, b=0.75):

      idf(term)  = ln((N - df + 0.5) / (df + 0.5) + 1)
      score(D,Q) = sum_term idf(term) * f*(k1+1) / (f + k1*(1 - b + b*|D|/avgdl))

  where f is the term frequency in D, |D| the document length in tokens,
  avgdl the average document length, N the corpus size, and df the number of
  documents containing the term.

- ``reciprocal_rank_fusion`` combines rankings that live on incompatible score
  scales (unbounded BM25 vs. bounded cosine) by using only rank position:
  ``rrf_score(doc) = sum_over_rankings 1 / (rrf_k + rank)`` with 1-based ranks.

- ``rerank`` reorders a small candidate set by model-judged relevance. The
  scoring function is injectable so callers (and tests) can supply their own;
  the default prompts ``settings.chat_model`` for a 0-10 relevance score.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from math import log

from .config import settings
from .store import Hit

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase ``text`` and split it into alphanumeric word tokens.

    Documents and queries must go through this same tokenizer, otherwise term
    matching silently breaks.
    """
    return _TOKEN_RE.findall(text.lower())


class BM25:
    """A BM25 keyword index over a fixed set of documents.

    All corpus statistics (per-document term frequencies and lengths, document
    frequencies, average length) are computed once at construction so scoring
    is a cheap lookup-and-sum.
    """

    def __init__(self, documents: Sequence[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._term_freqs: list[Counter[str]] = [Counter(tokenize(d)) for d in documents]
        self._doc_lens: list[int] = [sum(tf.values()) for tf in self._term_freqs]
        self._n_docs: int = len(self._term_freqs)
        self._avgdl: float = (
            sum(self._doc_lens) / self._n_docs if self._n_docs else 0.0
        )
        self._df: Counter[str] = Counter()
        for tf in self._term_freqs:
            self._df.update(tf.keys())

    def idf(self, term: str) -> float:
        """Inverse document frequency of ``term``; higher for rarer terms."""
        df = self._df.get(term, 0)
        return log((self._n_docs - df + 0.5) / (df + 0.5) + 1)

    def score(self, query: str, doc_index: int) -> float:
        """BM25 score of ``query`` against document ``doc_index``.

        A query sharing no terms with the document scores 0.0.
        """
        tf = self._term_freqs[doc_index]
        doc_len = self._doc_lens[doc_index]
        # Guard against an empty corpus / all-empty documents.
        length_ratio = doc_len / self._avgdl if self._avgdl else 0.0
        total = 0.0
        for term in tokenize(query):
            f = tf.get(term, 0)
            if not f:
                continue
            denom = f + self.k1 * (1 - self.b + self.b * length_ratio)
            total += self.idf(term) * f * (self.k1 + 1) / denom
        return total

    def search(self, query: str, k: int | None = None) -> list[tuple[int, float]]:
        """Rank every document against ``query``; return top-k (index, score).

        Highest score first; ``k`` defaults to ``settings.top_k``.
        """
        k = k or settings.top_k
        scored = [(i, self.score(query, i)) for i in range(self._n_docs)]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k]


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[int]], rrf_k: int | None = None
) -> list[tuple[int, float]]:
    """Fuse several best-first ranked lists of doc ids into one ranking.

    Each input ranking contributes ``1 / (rrf_k + rank)`` (rank is 1-based) to
    every doc it contains; docs ranked highly in multiple lists therefore beat
    docs ranked highly in only one. Raw scores are ignored, which makes the
    fusion scale-free. Returns (doc_id, rrf_score) sorted by score descending;
    ``rrf_k`` defaults to ``settings.rrf_k``.
    """
    if rrf_k is None:
        rrf_k = settings.rrf_k
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (rrf_k + rank)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


def _model_score(query: str, passage: str) -> float:
    """Ask the chat model to rate passage relevance to the query, 0-10.

    Parses the first number out of the reply; an unparseable reply scores 0.0
    so one bad completion cannot crash the whole rerank.
    """
    from .client import get_client  # deferred: pure callers never need a key

    prompt = (
        "Rate how relevant the passage is to the question on a scale of 0 to "
        "10, where 10 means it directly answers the question and 0 means it is "
        "unrelated. Reply with ONLY the number.\n\n"
        f"Question: {query}\n\nPassage: {passage}"
    )
    response = get_client().models.generate_content(
        model=settings.chat_model, contents=prompt
    )
    match = re.search(r"\d+(?:\.\d+)?", response.text or "")
    return float(match.group()) if match else 0.0


def rerank(query: str, hits: list[Hit], *, score_fn=None) -> list[Hit]:
    """Reorder candidate hits by judged relevance to ``query``, best first.

    ``score_fn(query, passage) -> float`` supplies the relevance judgment; when
    omitted, the chat model is prompted for a 0-10 score per candidate. Returns
    a new list; the input list is not mutated. The sort is stable, so equally
    scored hits keep their incoming order.
    """
    if score_fn is None:
        score_fn = _model_score
    return sorted(
        hits, key=lambda h: score_fn(query, h.chunk.text), reverse=True
    )
