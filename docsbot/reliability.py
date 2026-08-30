"""Retry transient failures with capped exponential backoff.

Model-API calls fail transiently — rate limits (429) and server errors (5xx)
usually succeed on a later attempt, while client errors (bad request, bad key)
fail identically every time. `retry_call` wraps a callable with that policy:
retry only what `is_retryable` classifies as transient, wait `backoff_delay
(attempt)` between attempts so a rate-limited server is never hammered in a
tight loop, and re-raise once `settings.max_retries` retries are exhausted so
callers see the real error instead of a hang.

The `sleep` function is injected so tests can record the backoff schedule
without touching the clock.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from .config import settings

T = TypeVar("T")

# HTTP statuses considered transient when an exception carries one (SDK errors
# often expose it as `.code` or `.status_code`).
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class RetryableError(Exception):
    """A transient failure (429/5xx-shaped) that is worth retrying.

    Raised directly for simulated transient errors; `is_retryable` also
    recognises SDK exceptions that carry a transient HTTP status.
    """


def backoff_delay(attempt: int) -> float:
    """Seconds to wait before retry number `attempt` (0-indexed).

    Doubles from `settings.backoff_base` each attempt, clamped to
    `settings.backoff_max` so a deep retry never stalls unbounded.
    """
    return min(settings.backoff_base * (2**attempt), settings.backoff_max)


def is_retryable(exc: BaseException) -> bool:
    """True if `exc` represents a transient failure worth retrying.

    `RetryableError` is always transient; other exceptions qualify only when
    they carry a transient HTTP status (429 or 5xx) as `.code` or
    `.status_code`. Everything else — bad requests, bad keys, programming
    bugs — must be re-raised immediately rather than retried.
    """
    if isinstance(exc, RetryableError):
        return True
    for attr in ("code", "status_code"):
        status = getattr(exc, attr, None)
        if isinstance(status, int) and status in _RETRYABLE_STATUSES:
            return True
    return False


def retry_call(fn: Callable[[], T], *, sleep: Callable[[float], None] = time.sleep) -> T:
    """Call `fn()`, retrying transient failures with exponential backoff.

    Returns `fn()`'s value on the first success. A retryable error triggers
    `sleep(backoff_delay(attempt))` and another attempt, up to
    `settings.max_retries` retries (`max_retries + 1` total attempts); the
    last error is re-raised once attempts are exhausted. A non-retryable
    error is re-raised immediately.

    `sleep` defaults to `time.sleep`; tests inject a recorder so the backoff
    schedule is observable without waiting.
    """
    for attempt in range(settings.max_retries + 1):
        try:
            return fn()
        except BaseException as exc:
            if not is_retryable(exc) or attempt >= settings.max_retries:
                raise
            sleep(backoff_delay(attempt))
    raise AssertionError("unreachable: loop returns or raises")
