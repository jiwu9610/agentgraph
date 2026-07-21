"""Retrieval specs — BM25, RRF fusion, and rerank.

BM25 and RRF are pure (no key). Rerank is tested with an injected fake scorer so
it needs no network either.

    pytest -m phase10
"""

import pytest

pytestmark = [pytest.mark.phase2, pytest.mark.phase10]

from docsbot.ingest import Chunk
from docsbot.retrieval import BM25, reciprocal_rank_fusion, rerank, tokenize
from docsbot.store import Hit


DOCS = [
    "the cat sat on the mat",
    "a dog chased the cat around the yard",
    "stock markets fell sharply on tuesday",
    "error code ERR_4021 means the disk is full",
]


# --- tokenizer --------------------------------------------------------------

def test_tokenize_lowercases_and_splits():
    assert tokenize("The Cat, SAT!") == ["the", "cat", "sat"]


# --- BM25 -------------------------------------------------------------------

def test_bm25_ranks_exact_term_match_first():
    bm = BM25(DOCS)
    results = bm.search("ERR_4021 disk", k=2)
    assert results[0][0] == 3  # the doc literally containing the error code wins


def test_bm25_rewards_rarer_terms():
    bm = BM25(DOCS)
    # "the" is in almost every doc (low idf); "markets" is rare (high idf).
    assert bm.idf("markets") > bm.idf("the")


def test_bm25_query_term_absent_scores_low():
    bm = BM25(DOCS)
    # A query with no shared terms should not rank doc 0 above a matching doc.
    results = dict(bm.search("airplane turbulence", k=4))
    # No real matches -> doc 0 has no positive signal.
    assert results.get(0, 0.0) == pytest.approx(0.0, abs=1e-9)


# --- Reciprocal Rank Fusion -------------------------------------------------

def test_rrf_rewards_agreement_across_rankings():
    # doc 5 is #1 in the first ranking and #2 in the second -> should win overall.
    vector_ranking = [5, 1, 2]
    bm25_ranking = [9, 5, 1]
    fused = reciprocal_rank_fusion([vector_ranking, bm25_ranking])
    assert fused[0][0] == 5
    # scores are sorted descending
    scores = [s for _, s in fused]
    assert scores == sorted(scores, reverse=True)


def test_rrf_single_ranking_preserves_order():
    fused = reciprocal_rank_fusion([[7, 3, 1]])
    assert [doc for doc, _ in fused] == [7, 3, 1]


# --- rerank (model judged, but with an injected fake scorer) ----------------

def test_rerank_sorts_by_injected_relevance():
    hits = [
        Hit(Chunk("irrelevant filler text", "a.md", 0), score=0.9),
        Hit(Chunk("the exact answer the user wanted", "b.md", 0), score=0.4),
    ]

    # Fake reranker: relevance = length of the passage's overlap with "answer".
    def fake_score(query, passage):
        return 10.0 if "answer" in passage else 1.0

    reranked = rerank("what is the answer?", hits, score_fn=fake_score)
    assert reranked[0].chunk.source == "b.md"   # promoted despite lower vector score
    assert [h.chunk.source for h in hits] == ["a.md", "b.md"]  # input not mutated
