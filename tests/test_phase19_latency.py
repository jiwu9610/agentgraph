"""Latency specs.  Run:  pytest -m phase19

Closes INC-001 (`incidents/INC-001-slow-answers.md`).

These assert on wall clock and on provider call *shape*, not on any particular
fix. Parallelising the reranker and batching it into one call both satisfy this
suite — the latency budget doesn't care which. (The cost specs will care, for a
different reason.)
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import assert_grounded, assert_ranked_by_relevance, p95
from docsbot.config import settings

pytestmark = [pytest.mark.phase4, pytest.mark.phase19]


QUESTIONS = [
    "Which service is the double-entry accounting core?",
    "What happens when risk-engine times out?",
    "Why is a payment intent written before calling the processor?",
    "What does edge-gateway do?",
    "How does webhook-dispatcher handle a failing merchant endpoint?",
    "What is stored in Redis and why?",
    "Which database uses synchronous replication?",
    "What stops authorization entirely if it goes down?",
]


def _cold_ask(service, question):
    """Measure the real work path, not a cache hit."""
    service.cache._data.clear()
    started = time.perf_counter()
    answer = service.ask(question)
    return answer, time.perf_counter() - started


# ----------------------------------------------------------------------
# the headline number
# ----------------------------------------------------------------------
def test_ask_p95_within_budget(service):
    """p95, not the mean. The mean hides exactly the users who are suffering."""
    latencies = [_cold_ask(service, q)[1] for q in QUESTIONS]
    observed = p95(latencies)
    assert observed < settings.budget_ask_p95_s, (
        f"p95 ask latency {observed:.3f}s exceeds budget "
        f"{settings.budget_ask_p95_s:.3f}s (latencies: "
        f"{[round(x, 3) for x in latencies]})"
    )


def test_query_is_embedded_exactly_once_per_ask(service, provider):
    """Embedding the same query twice is a whole round-trip for nothing."""
    service.ask(QUESTIONS[0])
    query_embeds = provider.calls_of("embed", tag="query_embed")
    assert len(query_embeds) == 1, (
        f"the query was embedded {len(query_embeds)} times in one ask; "
        "embed it once and reuse the vector"
    )


def test_ask_does_not_stack_up_serial_provider_calls(service, provider):
    """The ask must not be a chain of round-trips waiting on each other.

    Satisfied by batching the reranker into one call, or by running the
    per-candidate calls concurrently. Both are legitimate.
    """
    _, wall = _cold_ask(service, QUESTIONS[0])
    provider_time = sum(c.duration_s for c in provider.calls)
    n_calls = len(provider.calls)

    assert n_calls <= 4 or wall < provider_time * 0.6, (
        f"{n_calls} provider calls took {provider_time:.3f}s of provider time "
        f"and {wall:.3f}s of wall clock — they ran end to end. Batch them or "
        "run them concurrently."
    )


# ----------------------------------------------------------------------
# concurrency
# ----------------------------------------------------------------------
def test_concurrent_asks_do_not_serialize(service):
    """8 concurrent users must not wait in a queue behind each other.

    INC-001: a global lock around the request path meant 8 concurrent asks took
    ~8x as long as one. Whatever the lock was protecting, this is not the way.
    """
    questions = QUESTIONS[:8]

    service.cache._data.clear()
    serial_started = time.perf_counter()
    for q in questions:
        service.ask(q)
    serial = time.perf_counter() - serial_started

    service.cache._data.clear()
    concurrent_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(service.ask, questions))
    concurrent = time.perf_counter() - concurrent_started

    assert concurrent < serial * 0.5, (
        f"8 asks took {serial:.3f}s serially and {concurrent:.3f}s "
        f"concurrently — concurrency is buying almost nothing, so requests are "
        "queueing behind a shared lock"
    )


# ----------------------------------------------------------------------
# the health check
# ----------------------------------------------------------------------
def test_health_check_makes_no_provider_calls(fresh_service):
    """A liveness probe must never trigger the expensive startup work.

    INC-001: the LB's 2s health-check timeout fired while a cold instance
    embedded the corpus, so the LB killed the instance, and its replacement did
    exactly the same thing. Deploys flapped for ten minutes.
    """
    fresh_service.health()
    assert fresh_service.provider.call_count == 0, (
        f"health() made {fresh_service.provider.call_count} provider calls; "
        "it must be local and free"
    )


def test_health_check_is_fast(fresh_service):
    started = time.perf_counter()
    for _ in range(20):
        fresh_service.health()
    elapsed = time.perf_counter() - started
    assert elapsed < 0.05, f"20 health checks took {elapsed:.3f}s"


def test_health_check_reports_status(fresh_service):
    assert fresh_service.health()["status"] == "ok"


# ----------------------------------------------------------------------
# correctness guards — speed that breaks the product is not a fix
# ----------------------------------------------------------------------
def test_answers_are_still_grounded(service):
    for question in QUESTIONS[:4]:
        service.cache._data.clear()
        assert_grounded(service.ask(question))


def test_reranking_still_orders_by_relevance(service):
    answer = service.ask(QUESTIONS[0])
    assert_ranked_by_relevance(answer)


def test_retrieval_still_finds_the_right_document(service):
    """Ledger questions must cite the architecture doc, not just anything."""
    answer = service.ask("Which service is the append-only double-entry ledger?")
    sources = {c.source for c in answer.citations}
    assert any("architecture" in s for s in sources), (
        f"expected the architecture doc among citations, got {sorted(sources)}"
    )
