"""Shared fixtures for the performance specs.

Two ideas underpin these specs.

**The harness is the ground truth.** Every budget assertion reads
`FakeProvider`'s own counters — calls, tokens, dollars, wall clock. The code
under test cannot report its way to a passing test.

**Every perf spec is paired with a correctness guard.** The fastest possible
DocsBot returns "" instantly for every question and costs nothing. That is why
`assert_grounded()` exists and why it runs alongside the budgets: the goal is
to make a *working* product cheap, which is a much harder and much more
useful problem than making a broken one fast.
"""

from __future__ import annotations

import math

import pytest

from docsbot.perf.harness import FakeProvider
from docsbot.service.pipeline import DocsService, load_corpus


@pytest.fixture(scope="module")
def corpus() -> dict[str, str]:
    return load_corpus()


@pytest.fixture(scope="module")
def indexed(corpus):
    """A service with the corpus already indexed.

    Module-scoped so indexing runs once, not per test — the provider counters
    are reset before each test anyway, so tests still see a clean slate.
    """
    provider = FakeProvider()
    service = DocsService(provider)
    service.index(corpus)
    return service


@pytest.fixture
def service(indexed):
    """Clean per-test state: no recorded calls, cold cache, no sessions."""
    indexed.provider.reset()
    indexed.cache.hits = 0
    indexed.cache.misses = 0
    indexed.cache._data.clear()
    indexed.sessions._sessions.clear()
    return indexed


@pytest.fixture
def provider(service) -> FakeProvider:
    return service.provider


@pytest.fixture
def fresh_service(corpus):
    """An UNindexed service, for tests that measure indexing itself."""
    provider = FakeProvider()
    return DocsService(provider)


def p95(samples: list[float]) -> float:
    """Nearest-rank p95, defined here on purpose.

    The specs deliberately do not reuse the project's own `percentile` — a bug
    there would surface as a confusing failure over here, and each spec file
    should stand on its own.
    """
    if not samples:
        return 0.0
    ordered = sorted(samples)
    rank = math.ceil(0.95 * len(ordered))
    return ordered[max(1, min(len(ordered), rank)) - 1]


# ----------------------------------------------------------------------
# correctness guards
# ----------------------------------------------------------------------
QUESTIONS = [
    "Which service is the double-entry accounting core?",
    "What happens when risk-engine times out?",
    "Why is a payment intent written before calling the processor?",
    "What does edge-gateway do?",
    "How does webhook-dispatcher handle a failing merchant endpoint?",
]


def assert_grounded(answer, *, expect_source: str | None = None) -> None:
    """The product still has to work.

    An answer must be non-empty, must carry citations, and those citations must
    point at real documents. Speed and thrift that cost you this are not wins.
    """
    assert answer.text, "answer text is empty"
    assert answer.citations, "answer carries no citations"
    for c in answer.citations:
        assert c.source.endswith(".md"), f"bogus citation source {c.source!r}"
    if expect_source is not None:
        sources = {c.source for c in answer.citations}
        assert expect_source in sources, (
            f"expected {expect_source} among citations, got {sorted(sources)}"
        )


def assert_ranked_by_relevance(answer) -> None:
    """Reranking must still rank. Citation scores descend."""
    scores = [c.score for c in answer.citations]
    assert scores == sorted(scores, reverse=True), (
        f"citations are not in descending relevance order: {scores}"
    )
