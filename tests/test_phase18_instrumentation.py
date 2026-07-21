"""Metrics instrumentation specs — the cockpit.  Run:  pytest -m phase18

Pure logic. No provider, no network, no key. If these pass, the instrument can
be trusted, which is the precondition for everything downstream: the numbers
every later diagnosis rests on come from here.
"""

from __future__ import annotations

import threading
import time

import pytest

from docsbot.perf.metrics import DEFAULT_MAX_SAMPLES, Metrics, Span, percentile

pytestmark = [pytest.mark.phase4, pytest.mark.phase18]


# ----------------------------------------------------------------------
# percentiles
# ----------------------------------------------------------------------
def test_percentile_of_empty_is_zero():
    assert percentile([], 95) == 0.0


def test_percentile_single_sample():
    assert percentile([4.2], 50) == 4.2
    assert percentile([4.2], 95) == 4.2


def test_percentile_nearest_rank():
    # ceil(p/100 * n)-th smallest, 1-indexed.
    samples = [1.0, 2.0, 3.0, 4.0]
    assert percentile(samples, 50) == 2.0    # ceil(0.50*4) = 2
    assert percentile(samples, 75) == 3.0    # ceil(0.75*4) = 3
    assert percentile(samples, 100) == 4.0


def test_percentile_p95_over_100_samples():
    samples = [float(i) for i in range(1, 101)]
    assert percentile(samples, 95) == 95.0


def test_percentile_clamps_low_p_to_first_element():
    assert percentile([5.0, 6.0, 7.0], 0) == 5.0


def test_percentile_handles_unsorted_input_without_mutating_it():
    samples = [9.0, 1.0, 5.0]
    original = list(samples)
    assert percentile(samples, 50) == 5.0
    assert samples == original, "percentile() must not reorder the caller's list"


def test_percentile_is_what_users_feel_not_the_mean():
    """The point of the design: 90 fast requests and 10 slow ones."""
    samples = [0.05] * 90 + [5.0] * 10
    assert percentile(samples, 50) == 0.05
    assert percentile(samples, 95) == 5.0


# ----------------------------------------------------------------------
# spans
# ----------------------------------------------------------------------
def test_span_duration_and_self_time():
    parent = Span(name="parent", start=0.0, end=10.0)
    child = Span(name="child", start=1.0, end=7.0)
    parent.children.append(child)

    assert parent.duration_s == 10.0
    assert child.duration_s == 6.0
    # Self time is what localizes a bottleneck: the parent is only responsible
    # for 4 of its 10 seconds.
    assert parent.self_time_s == 4.0
    assert child.self_time_s == 6.0


def test_open_span_has_zero_duration():
    assert Span(name="open", start=1.0).duration_s == 0.0


def test_span_records_nesting():
    m = Metrics()
    with m.span("outer"):
        with m.span("inner"):
            pass

    roots = m.roots()
    assert len(roots) == 1
    assert roots[0].name == "outer"
    assert [c.name for c in roots[0].children] == ["inner"]


def test_span_closes_even_when_body_raises():
    """An exception is exactly when the timing matters most."""
    m = Metrics()
    with pytest.raises(ValueError):
        with m.span("boom"):
            raise ValueError("kaboom")

    roots = m.roots()
    assert len(roots) == 1
    assert roots[0].end is not None, "span was left open after an exception"


def test_span_records_its_duration_as_a_sample():
    m = Metrics()
    for _ in range(3):
        with m.span("stage"):
            time.sleep(0.001)
    assert len(m.samples("stage")) == 3
    assert m.percentile_of("stage", 95) > 0


def test_sibling_spans_are_not_nested():
    m = Metrics()
    with m.span("a"):
        pass
    with m.span("b"):
        pass
    assert [r.name for r in m.roots()] == ["a", "b"]
    assert all(not r.children for r in m.roots())


def test_spans_in_different_threads_do_not_nest():
    """Two threads timing the same stage are siblings, not parent and child.

    Get this wrong and every concurrency measurement is nonsense.
    """
    m = Metrics()
    barrier = threading.Barrier(2)

    def work():
        barrier.wait()
        with m.span("concurrent"):
            time.sleep(0.005)

    threads = [threading.Thread(target=work) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    roots = m.roots()
    assert len(roots) == 2, f"expected 2 sibling roots, got {len(roots)}"
    assert all(not r.children for r in roots)


# ----------------------------------------------------------------------
# counters and samples
# ----------------------------------------------------------------------
def test_count_accumulates():
    m = Metrics()
    m.count("calls")
    m.count("calls", 4)
    assert m.counters()["calls"] == 5


def test_counters_returns_a_copy():
    m = Metrics()
    m.count("x")
    snapshot = m.counters()
    snapshot["x"] = 999
    assert m.counters()["x"] == 1


def test_counters_do_not_lose_increments_under_concurrency():
    m = Metrics()

    def bump():
        for _ in range(500):
            m.count("hits")

    threads = [threading.Thread(target=bump) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert m.counters()["hits"] == 4000


def test_observe_is_bounded_and_drops_oldest():
    """INC-004: instrumentation that grows forever is itself a leak."""
    m = Metrics(max_samples=100)
    for i in range(1000):
        m.observe("latency", float(i))

    samples = m.samples("latency")
    assert len(samples) == 100, "sample storage is unbounded"
    # Oldest dropped, newest kept.
    assert max(samples) == 999.0
    assert min(samples) == 900.0


def test_default_sample_cap_is_finite():
    assert DEFAULT_MAX_SAMPLES > 0
    m = Metrics()
    for i in range(DEFAULT_MAX_SAMPLES + 50):
        m.observe("x", float(i))
    assert len(m.samples("x")) == DEFAULT_MAX_SAMPLES


# ----------------------------------------------------------------------
# rollup
# ----------------------------------------------------------------------
def test_stage_table_rolls_up_nested_spans():
    m = Metrics()
    with m.span("ask"):
        with m.span("retrieve"):
            time.sleep(0.01)
        with m.span("generate"):
            time.sleep(0.01)

    table = m.stage_table()
    assert set(table) == {"ask", "retrieve", "generate"}
    assert table["ask"]["count"] == 1

    # `ask` spent nearly all its time inside children, so its SELF time is
    # small even though its total is the largest. This is the distinction that
    # shows which stage to actually go fix.
    assert table["ask"]["total_s"] >= table["retrieve"]["total_s"]
    assert table["ask"]["self_s"] < table["retrieve"]["self_s"]


def test_stage_table_counts_repeated_stages():
    m = Metrics()
    for _ in range(5):
        with m.span("rerank"):
            pass
    assert m.stage_table()["rerank"]["count"] == 5


def test_stage_table_reports_p95_per_stage():
    m = Metrics()
    for _ in range(10):
        with m.span("call"):
            time.sleep(0.001)
    row = m.stage_table()["call"]
    assert row["p95_s"] > 0
    assert row["p95_s"] <= row["total_s"]


def test_report_has_stages_and_counters():
    m = Metrics()
    with m.span("ask"):
        m.count("cache_miss")
    report = m.report()
    assert "stages" in report and "counters" in report
    assert report["counters"]["cache_miss"] == 1
    assert "ask" in report["stages"]


def test_reset_clears_everything():
    m = Metrics()
    with m.span("ask"):
        m.count("x")
    m.observe("y", 1.0)
    m.reset()

    assert m.roots() == []
    assert m.counters() == {}
    assert m.samples("y") == []
    assert m.stage_table() == {}
