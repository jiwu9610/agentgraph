"""Resilience specs — failure and waste under load.  Run:  pytest -m phase21

Closes INC-003 (`incidents/INC-003-outage-cost-spike.md`).

A three-minute upstream blip became a thirteen-minute incident at 30x cost.
The blip is not the bug. The response to the blip is the bug.

These tests run instantly because `sleep` is injected — the same seam the
reliability specs use. A backoff under test is never actually waited out.
"""

from __future__ import annotations

import time

import pytest

from docsbot.config import settings
from docsbot.perf.harness import FakeProvider, ProviderError
from docsbot.service.resilience import (
    CircuitBreaker,
    CircuitBreakerOpen,
    backoff_delay,
    call_with_timeout,
    is_retryable,
    map_with_retry,
    retry_call,
)

pytestmark = [pytest.mark.phase4, pytest.mark.phase21]


class RecordingSleep:
    """A fake sleep that records delays instead of spending them."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


# ----------------------------------------------------------------------
# what is worth retrying
# ----------------------------------------------------------------------
@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_transient_errors_are_retryable(code):
    assert is_retryable(ProviderError("boom", code=code)) is True


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_client_errors_are_not_retryable(code):
    """A malformed request retried four times is four identical failures."""
    assert is_retryable(ProviderError("bad", code=code)) is False


def test_unknown_exceptions_are_not_retryable():
    assert is_retryable(ValueError("a programming bug")) is False


def test_non_retryable_error_costs_exactly_one_call():
    provider = FakeProvider()
    provider.fail_next(10, code=400)
    sleep = RecordingSleep()

    with pytest.raises(ProviderError):
        retry_call(lambda: provider.chat("hello"), sleep=sleep)

    assert provider.call_count == 1, (
        f"a 400 cost {provider.call_count} provider calls; it will fail "
        "identically every time, so it must cost exactly one"
    )
    assert sleep.delays == [], "we slept before giving up on an unretryable error"


def test_transient_failure_recovers_without_surfacing_an_error():
    provider = FakeProvider()
    provider.fail_next(2, code=429)
    sleep = RecordingSleep()

    result = retry_call(lambda: provider.chat("CLASSIFY:hi"), sleep=sleep)

    assert result, "a recoverable call should still return a result"
    assert provider.call_count == 3, "expected 2 failures then 1 success"


def test_persistent_failure_is_bounded_then_gives_up():
    provider = FakeProvider()
    provider.fail_next(1000, code=429)
    sleep = RecordingSleep()

    with pytest.raises(ProviderError):
        retry_call(lambda: provider.chat("hello"), sleep=sleep)

    max_calls = settings.max_retries + 1
    assert provider.call_count == max_calls, (
        f"made {provider.call_count} calls against a permanently failing "
        f"provider; the bound is {max_calls}"
    )


# ----------------------------------------------------------------------
# jitter — the 20x spike was synchronization, not volume
# ----------------------------------------------------------------------
def test_backoff_is_bounded_by_the_cap():
    for attempt in range(10):
        assert 0.0 <= backoff_delay(attempt) <= settings.backoff_max


def test_backoff_grows_with_attempt_on_average():
    early = sum(backoff_delay(0) for _ in range(200)) / 200
    later = sum(backoff_delay(3) for _ in range(200)) / 200
    assert later > early, "backoff must still grow exponentially on average"


def test_backoff_is_jittered():
    """INC-003: deterministic backoff builds a metronome.

    Every client rate-limited at the same instant computes the same delay,
    sleeps the same duration, and hits the provider again at the same
    millisecond. That is where the 20x call spike came from.
    """
    delays = {backoff_delay(2) for _ in range(50)}
    assert len(delays) > 1, (
        "backoff_delay is deterministic — 500 clients failing together will "
        "retry in lockstep and hammer the provider in synchronized waves"
    )


# ----------------------------------------------------------------------
# timeouts — where the 90-second hangs came from
# ----------------------------------------------------------------------
def test_timeout_raises_instead_of_hanging():
    def hangs():
        time.sleep(5.0)
        return "never"

    started = time.perf_counter()
    with pytest.raises(TimeoutError):
        call_with_timeout(hangs, 0.05)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, (
        f"call_with_timeout waited {elapsed:.2f}s for a 0.05s timeout"
    )


def test_timeout_returns_the_value_when_fast_enough():
    assert call_with_timeout(lambda: "ok", 1.0) == "ok"


def test_no_timeout_configured_still_works():
    assert call_with_timeout(lambda: "ok", None) == "ok"


def test_hanging_provider_call_does_not_park_the_caller():
    provider = FakeProvider()
    provider.hang_next(1, seconds=3.0)

    started = time.perf_counter()
    with pytest.raises(TimeoutError):
        call_with_timeout(lambda: provider.chat("hello"), 0.05)
    assert time.perf_counter() - started < 1.0


# ----------------------------------------------------------------------
# the circuit breaker
# ----------------------------------------------------------------------
def test_breaker_starts_closed():
    assert CircuitBreaker(threshold=3).allow() is True


def test_breaker_opens_after_consecutive_failures():
    breaker = CircuitBreaker(threshold=3, reset_after_s=30.0)
    for _ in range(3):
        breaker.record_failure()
    assert breaker.allow() is False, "breaker never opened"
    assert breaker.is_open is True


def test_success_resets_the_failure_run():
    breaker = CircuitBreaker(threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.allow() is True, "consecutive means consecutive"


def test_open_breaker_short_circuits_without_touching_the_provider():
    """The point of a breaker: stop paying to talk to something that is down."""
    provider = FakeProvider()
    provider.fail_next(1000, code=503)
    breaker = CircuitBreaker(threshold=settings.breaker_threshold,
                             reset_after_s=30.0)
    sleep = RecordingSleep()

    with pytest.raises((ProviderError, CircuitBreakerOpen)):
        retry_call(lambda: provider.chat("x"), sleep=sleep, breaker=breaker)

    calls_before = provider.call_count
    for _ in range(20):
        with pytest.raises(CircuitBreakerOpen):
            retry_call(lambda: provider.chat("x"), sleep=sleep, breaker=breaker)

    assert provider.call_count == calls_before, (
        f"the open breaker still let {provider.call_count - calls_before} "
        "calls through to a provider we know is down"
    )


def test_breaker_allows_a_trial_call_after_the_reset_window():
    breaker = CircuitBreaker(threshold=2, reset_after_s=0.05)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.allow() is False

    time.sleep(0.06)
    assert breaker.allow() is True, "breaker never half-opens, so it never recovers"


# ----------------------------------------------------------------------
# the retry unit
# ----------------------------------------------------------------------
def test_one_failing_item_does_not_rerun_the_whole_batch():
    """INC-003: retrying the batch re-pays for every item that already
    succeeded. At a 40% failure rate a large batch may never finish at all."""
    attempts: dict[int, int] = {}
    sleep = RecordingSleep()

    def flaky(item: int) -> str:
        attempts[item] = attempts.get(item, 0) + 1
        if item == 3 and attempts[item] == 1:
            raise ProviderError("transient", code=429)
        return f"ok-{item}"

    results = map_with_retry(flaky, list(range(6)), sleep=sleep)

    assert results == [f"ok-{i}" for i in range(6)]
    total = sum(attempts.values())
    assert total == 7, (
        f"6 items with one transient failure took {total} attempts; only the "
        f"failing item should be retried (per-item attempts: {attempts})"
    )


# ----------------------------------------------------------------------
# the bottom line
# ----------------------------------------------------------------------
def test_spend_during_an_outage_is_bounded():
    """40% failure rate across a burst — total calls must stay bounded."""
    provider = FakeProvider()
    breaker = CircuitBreaker(threshold=settings.breaker_threshold,
                             reset_after_s=30.0)
    sleep = RecordingSleep()
    provider.fail_next(10_000, code=503)

    for _ in range(50):
        try:
            retry_call(lambda: provider.chat("x"), sleep=sleep, breaker=breaker)
        except (ProviderError, CircuitBreakerOpen):
            pass

    ceiling = settings.breaker_threshold + settings.max_retries + 1
    assert provider.call_count <= ceiling, (
        f"50 requests against a dead provider cost {provider.call_count} "
        f"provider calls; with a breaker the ceiling is about {ceiling}"
    )
