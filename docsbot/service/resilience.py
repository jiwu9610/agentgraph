"""Retries, timeouts, and the circuit breaker.

A system retries where a demo crashes — but a retry policy is a *spending*
policy, and the instrumented baseline spent badly.

In the scenario the INC-003 ticket describes
(`incidents/INC-003-outage-cost-spike.md`): the provider was degraded for
three minutes. Three minutes. In that window the baseline service made tens of
thousands of calls, spent 30x its normal hourly cost, and users who would have
seen a fast error instead watched a spinner for 90 seconds. The provider's own
status page said "partial outage." The service said nothing, because it was
too busy retrying to notice.

Every knob below is individually defensible. Together they are a bill.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import TypeVar

from ..config import settings
from ..perf.harness import ProviderError

T = TypeVar("T")

# One shared pool so a timeout doesn't cost a thread spin-up per call.
_TIMEOUT_POOL = ThreadPoolExecutor(max_workers=32,
                                   thread_name_prefix="docsbot-timeout")


class CircuitBreakerOpen(Exception):
    """Raised instead of calling a provider we believe is down."""


def is_retryable(exc: BaseException) -> bool:
    """Should we spend another call on this?

    429 = slow down, transient. 5xx = their fault, transient. Both worth a retry.
    400 = our request is malformed. 401 = our key is wrong. Retrying either one
    produces the identical failure, three more times, for money.
    """
    if isinstance(exc, TimeoutError):
        return True          # the request may simply have been unlucky
    code = getattr(exc, "code", None)
    if code is None:
        return False
    return code == 429 or 500 <= code < 600


def backoff_delay(attempt: int) -> float:
    """Seconds to wait before retry number `attempt` (0-indexed).

    Capped exponential growth: base * 2**attempt, clamped to backoff_max.

    Uses FULL JITTER: a uniform random point in [0, capped_delay]. Without it,
    500 clients rate-limited at the same instant all sleep the same duration and
    all retry at the same millisecond — the herd reconverges on the provider at
    full force and gets rate-limited again. Jitter spreads the retries out so
    the provider sees a smooth trickle instead of a wall.
    """
    capped = min(settings.backoff_base * (2 ** attempt), settings.backoff_max)
    return random.uniform(0.0, capped)


class CircuitBreaker:
    """Stop calling a provider that is clearly down.

    The point of a breaker is to convert a slow cascading failure into a fast,
    cheap, obvious one. After `threshold` consecutive failures it OPENS and
    rejects calls immediately without touching the network. After
    `reset_after_s` it allows a single trial call through (half-open) and
    closes again if that succeeds.
    """

    def __init__(self, threshold: int = 5, reset_after_s: float = 30.0) -> None:
        self.threshold = threshold
        self.reset_after_s = reset_after_s
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    def allow(self) -> bool:
        """True if a call may proceed."""
        with self._lock:
            if self._opened_at is None:
                return True
            if time.monotonic() - self._opened_at >= self.reset_after_s:
                # Half-open: let exactly one trial call through.
                self._opened_at = None
                self._consecutive_failures = self.threshold - 1
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.threshold and self._opened_at is None:
                self._opened_at = time.monotonic()

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._opened_at is not None

    def state(self) -> dict:
        with self._lock:
            return {"consecutive_failures": self._consecutive_failures,
                    "open": self._opened_at is not None}


def call_with_timeout(fn: Callable[[], T], timeout_s: float | None) -> T:
    """Run `fn`, giving up after `timeout_s` seconds.

    A provider that returns an error is easy. A provider that accepts your
    connection and then says nothing is what actually takes a service down:
    every worker ends up parked on a socket that will never answer, and the
    service stops serving anyone — including the users whose requests were fine.

    Caveat worth knowing: Python cannot kill a running thread, so the abandoned
    call finishes in the background. This bounds what the *caller* waits for,
    which is what protects your worker pool. The complete fix is a timeout on
    the provider client's own socket — do both in real systems.
    """
    if timeout_s is None:
        return fn()

    future = _TIMEOUT_POOL.submit(fn)
    try:
        return future.result(timeout=timeout_s)
    except FutureTimeout:
        future.cancel()
        raise TimeoutError(f"provider call exceeded {timeout_s}s")


def retry_call(
    fn: Callable[[], T],
    *,
    sleep: Callable[[float], None] = time.sleep,
    timeout_s: float | None = None,
    breaker: CircuitBreaker | None = None,
) -> T:
    """Call `fn`, retrying transient failures with capped exponential backoff.

    `sleep` is injected so tests run instantly.
    """
    last_exc: BaseException | None = None

    for attempt in range(settings.max_retries + 1):
        if breaker is not None and not breaker.allow():
            raise CircuitBreakerOpen("provider circuit is open")
        try:
            result = call_with_timeout(fn, timeout_s)
        except Exception as exc:
            last_exc = exc
            if breaker is not None:
                breaker.record_failure()
            if not is_retryable(exc):
                raise
            if attempt < settings.max_retries:
                sleep(backoff_delay(attempt))
            continue
        if breaker is not None:
            breaker.record_success()
        return result

    assert last_exc is not None
    raise last_exc


def map_with_retry(
    fn: Callable[[T], object],
    items: Sequence[T],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> list:
    """Apply `fn` to every item, retrying transient failures.

    Used for batch work: embedding a page of chunks, scoring a page of
    candidates. The choice of retry *unit* matters — see below.
    """
    # Retry the ITEM, not the batch. If item 47 of 50 fails once, retrying the
    # whole batch re-pays for the 46 that already succeeded — and if failures
    # are at all common, a large batch may never complete at all.
    return [retry_call(lambda it=item: fn(it), sleep=sleep) for item in items]
