"""Leak and regression-gate specs.  Run:  pytest -m phase22

Closes INC-004 (`incidents/INC-004-the-sawtooth.md`).

Two halves, and the second one matters more.

**Stop the leaks.** Anything that grows without bound as traffic grows is a
leak — including the metrics instrumentation used to find leaks.

**Build the gate.** The latency, cost, and retry-storm fixes are in place, but
none of them stops a regression from coming back. A target number in a wiki is
a wish; a target number in CI is a constraint.
"""

from __future__ import annotations

import time

import pytest

from docsbot.config import settings
from docsbot.perf.budgets import Budget, Violation, check, default_budgets
from docsbot.perf.metrics import Metrics
from docsbot.service.sessions import SessionStore

pytestmark = [pytest.mark.phase4, pytest.mark.phase22]


# ----------------------------------------------------------------------
# the session store — the sawtooth
# ----------------------------------------------------------------------
def test_expired_sessions_are_purged_on_write():
    """`purge_expired` existed and was never called. Dead code is not a policy."""
    store = SessionStore(ttl_s=0.05, max_sessions=1000)
    store.append("old-session", "hello", "hi there")
    assert len(store) == 1

    time.sleep(0.06)
    store.append("new-session", "hello", "hi there")

    assert len(store) == 1, (
        f"expected the expired session to be gone, store holds {len(store)}"
    )
    assert store.history("old-session") == []
    assert store.history("new-session") != []


def test_session_count_is_capped():
    """TTL alone is not enough: a burst of distinct ids inside the TTL window
    still fills memory, and a retry loop with a fresh uuid produces exactly
    that."""
    store = SessionStore(ttl_s=3600.0, max_sessions=10)
    for i in range(200):
        store.append(f"session-{i}", "q", "a")

    assert len(store) <= 10, (
        f"200 sessions with a cap of 10 left {len(store)} in memory"
    )


def test_cap_evicts_least_recently_seen_first():
    store = SessionStore(ttl_s=3600.0, max_sessions=3)
    for i in range(3):
        store.append(f"s{i}", "q", "a")
        time.sleep(0.001)

    store.append("s0", "another", "answer")   # s0 is now the most recent
    store.append("s-new", "q", "a")           # forces one eviction

    assert len(store) <= 3
    assert store.history("s0"), "the most recently used session was evicted"


def test_active_session_survives():
    """Don't fix the leak by throwing away conversations people are having."""
    store = SessionStore(ttl_s=0.05, max_sessions=100)
    store.append("chatty", "q1", "a1")
    for _ in range(3):
        time.sleep(0.02)
        store.append("chatty", "q", "a")
    assert len(store.history("chatty")) == 4


def test_service_sessions_do_not_grow_without_bound(service):
    """The whole-system version: many distinct callers, bounded memory."""
    service.sessions.max_sessions = 25
    for i in range(120):
        service.sessions.append(f"user-{i}", "question", "answer")
    assert len(service.sessions) <= 25


# ----------------------------------------------------------------------
# the instrumentation must not itself leak
# ----------------------------------------------------------------------
def test_metrics_sample_storage_is_bounded():
    """'That would be a hell of a thing to page ourselves about.'"""
    m = Metrics(max_samples=50)
    for i in range(20_000):
        m.observe("ask_latency", float(i))
    assert len(m.samples("ask_latency")) == 50


def test_metrics_percentiles_still_work_when_bounded():
    m = Metrics(max_samples=100)
    for i in range(5_000):
        m.observe("x", float(i))
    # Percentiles over the retained window remain meaningful.
    assert m.percentile_of("x", 95) >= m.percentile_of("x", 50)


# ----------------------------------------------------------------------
# budgets
# ----------------------------------------------------------------------
def test_default_budgets_cover_the_four_metrics():
    names = {b.name for b in default_budgets()}
    assert names == {
        "ask.p95_s", "ask.input_tokens", "ask.provider_calls", "ask.usd",
    }


def test_default_budgets_read_from_settings():
    by_name = {b.name: b.limit for b in default_budgets()}
    assert by_name["ask.p95_s"] == settings.budget_ask_p95_s
    assert by_name["ask.input_tokens"] == settings.budget_ask_input_tokens
    assert by_name["ask.provider_calls"] == settings.budget_ask_provider_calls
    assert by_name["ask.usd"] == settings.budget_ask_usd


def test_check_returns_nothing_when_within_budget():
    budgets = [Budget("ask.p95_s", 1.0), Budget("ask.usd", 0.01)]
    assert check({"ask.p95_s": 0.4, "ask.usd": 0.001}, budgets) == []


def test_exactly_at_the_limit_passes():
    assert check({"ask.p95_s": 1.0}, [Budget("ask.p95_s", 1.0)]) == []


def test_check_flags_a_violation():
    violations = check({"ask.p95_s": 2.5}, [Budget("ask.p95_s", 1.0)])
    assert len(violations) == 1
    assert violations[0].observed == 2.5
    assert violations[0].budget.name == "ask.p95_s"


def test_violations_are_worst_first():
    """Fix the 5x regression before the 1.1x one."""
    budgets = [Budget("a", 1.0), Budget("b", 1.0), Budget("c", 1.0)]
    violations = check({"a": 1.2, "b": 5.0, "c": 2.0}, budgets)
    assert [v.budget.name for v in violations] == ["b", "c", "a"]


def test_missing_metrics_are_skipped_not_failed():
    """A gate that fails on absent data trains people to ignore the gate."""
    assert check({}, [Budget("ask.p95_s", 1.0)]) == []


def test_overage_ratio_is_observed_over_limit():
    v = Violation(budget=Budget("ask.usd", 0.001), observed=0.003)
    assert v.overage_ratio == pytest.approx(3.0)


def test_violation_message_names_the_numbers():
    message = str(Violation(budget=Budget("ask.p95_s", 0.15, "s"), observed=0.42))
    assert "ask.p95_s" in message
    assert "0.42" in message
    assert "0.15" in message


# ----------------------------------------------------------------------
# the gate itself — this is the test that fails the build on a regression
# ----------------------------------------------------------------------
def measure_ask(service, provider, questions: list[str]) -> dict[str, float]:
    """Run a small load and return the metrics the budgets are written against."""
    from conftest import p95

    provider.reset()
    latencies = []
    for q in questions:
        service.cache._data.clear()
        started = time.perf_counter()
        service.ask(q)
        latencies.append(time.perf_counter() - started)

    n = len(questions)
    return {
        "ask.p95_s": p95(latencies),
        "ask.input_tokens": provider.input_tokens / n,
        "ask.provider_calls": provider.call_count / n,
        "ask.usd": provider.usd / n,
    }


def test_the_pipeline_is_within_every_budget(service, provider):
    """The regression gate. If this fails, something got slower or pricier."""
    questions = [
        "Which service is the double-entry accounting core?",
        "What happens when risk-engine times out?",
        "What does edge-gateway do?",
        "Which database uses synchronous replication?",
        "What is stored in Redis and why?",
    ]
    observed = measure_ask(service, provider, questions)
    violations = check(observed)

    assert violations == [], (
        "performance budget exceeded:\n  "
        + "\n  ".join(str(v) for v in violations)
        + f"\n\nobserved: { {k: round(v, 6) for k, v in observed.items()} }"
    )


def test_the_gate_actually_catches_a_regression():
    """A gate nobody has ever seen fail is a gate nobody should trust."""
    regressed = {
        "ask.p95_s": settings.budget_ask_p95_s * 3,
        "ask.input_tokens": settings.budget_ask_input_tokens * 4,
        "ask.provider_calls": settings.budget_ask_provider_calls,
        "ask.usd": settings.budget_ask_usd * 2,
    }
    violations = check(regressed)
    names = {v.budget.name for v in violations}
    assert names == {"ask.p95_s", "ask.input_tokens", "ask.usd"}
    assert violations[0].budget.name == "ask.input_tokens", "worst first"
