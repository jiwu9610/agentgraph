"""Reliability specs — retries + exponential backoff.

Pure logic, no network: we inject a flaky function and a fake `sleep` that
records delays instead of waiting, so the whole suite runs in milliseconds.

    pytest -m phase9
"""

import pytest

pytestmark = [pytest.mark.phase2, pytest.mark.phase9]

from docsbot import reliability
from docsbot.config import settings
from docsbot.reliability import (
    RetryableError,
    backoff_delay,
    is_retryable,
    retry_call,
)


class FakeSleep:
    """Records the delays it was asked to wait, without waiting."""

    def __init__(self):
        self.delays = []

    def __call__(self, seconds):
        self.delays.append(seconds)


# --- backoff schedule -------------------------------------------------------

def test_backoff_grows_exponentially_then_caps():
    base, cap = settings.backoff_base, settings.backoff_max
    assert backoff_delay(0) == pytest.approx(base)
    assert backoff_delay(1) == pytest.approx(base * 2)
    assert backoff_delay(2) == pytest.approx(base * 4)
    # Far-out attempts are clamped to the cap, never unbounded.
    assert backoff_delay(50) == pytest.approx(cap)


# --- retry classification ---------------------------------------------------

def test_retryable_vs_not():
    assert is_retryable(RetryableError("429 slow down")) is True
    assert is_retryable(ValueError("bad input")) is False  # not transient


# --- the retry loop ---------------------------------------------------------

def test_succeeds_first_try_no_sleep():
    sleep = FakeSleep()
    result = retry_call(lambda: 42, sleep=sleep)
    assert result == 42
    assert sleep.delays == []  # success never sleeps


def test_retries_then_succeeds():
    sleep = FakeSleep()
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:           # fail twice, then succeed
            raise RetryableError("429")
        return "ok"

    assert retry_call(flaky, sleep=sleep) == "ok"
    assert calls["n"] == 3
    # Backed off before each retry: two waits, exponentially spaced.
    assert sleep.delays == [backoff_delay(0), backoff_delay(1)]


def test_non_retryable_raises_immediately():
    sleep = FakeSleep()

    def bad():
        raise ValueError("malformed request")

    with pytest.raises(ValueError):
        retry_call(bad, sleep=sleep)
    assert sleep.delays == []  # never retried a non-transient error


def test_gives_up_after_max_retries():
    sleep = FakeSleep()
    calls = {"n": 0}

    def always_429():
        calls["n"] += 1
        raise RetryableError("429 forever")

    with pytest.raises(RetryableError):
        retry_call(always_429, sleep=sleep)
    # max_retries retries on top of the first attempt.
    assert calls["n"] == settings.max_retries + 1
    assert len(sleep.delays) == settings.max_retries
