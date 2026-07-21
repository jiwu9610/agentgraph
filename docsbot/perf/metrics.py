"""Thread-safe in-process metrics.

Nested spans with self time (time in a stage minus time in its child stages),
locked counters, nearest-rank percentiles, and bounded sample storage that
drops the oldest values — recent behaviour is what gets debugged.

Incident-by-incident analysis: PART4_POSTMORTEMS.md.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

DEFAULT_MAX_SAMPLES = 4096


def percentile(samples: list[float], p: float) -> float:
    """Nearest-rank percentile: the ceil(p/100 * n)-th smallest, 1-indexed."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    n = len(ordered)
    rank = math.ceil(p / 100.0 * n)
    rank = max(1, min(n, rank))
    return ordered[rank - 1]


@dataclass
class Span:
    name: str
    start: float
    end: float | None = None
    children: list["Span"] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        if self.end is None:
            return 0.0
        return self.end - self.start

    @property
    def self_time_s(self) -> float:
        return max(0.0, self.duration_s - sum(c.duration_s for c in self.children))


class Metrics:
    """Spans, counters, and histograms for one process. Thread-safe."""

    def __init__(self, max_samples: int = DEFAULT_MAX_SAMPLES) -> None:
        self.max_samples = max_samples
        self._lock = threading.Lock()
        self._local = threading.local()
        self._roots: list[Span] = []
        self._counters: dict[str, int] = {}
        self._samples: dict[str, list[float]] = {}

    # ------------------------------------------------------------------
    def _stack(self) -> list[Span]:
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = []
            self._local.stack = stack
        return stack

    @contextmanager
    def span(self, name: str) -> Iterator[Span]:
        stack = self._stack()
        sp = Span(name=name, start=time.perf_counter())
        stack.append(sp)
        try:
            yield sp
        finally:
            sp.end = time.perf_counter()
            # Pop *this* span even if something below us misbehaved.
            if stack and stack[-1] is sp:
                stack.pop()
            elif sp in stack:
                stack.remove(sp)
            if stack:
                stack[-1].children.append(sp)
            else:
                with self._lock:
                    self._roots.append(sp)
            self.observe(name, sp.duration_s)

    def count(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + n

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            bucket = self._samples.setdefault(name, [])
            bucket.append(value)
            if len(bucket) > self.max_samples:
                # Drop oldest; recent behaviour is what we're debugging.
                del bucket[: len(bucket) - self.max_samples]

    # ------------------------------------------------------------------
    def counters(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)

    def samples(self, name: str) -> list[float]:
        with self._lock:
            return list(self._samples.get(name, []))

    def percentile_of(self, name: str, p: float) -> float:
        return percentile(self.samples(name), p)

    def roots(self) -> list[Span]:
        with self._lock:
            return list(self._roots)

    # ------------------------------------------------------------------
    def _walk(self) -> Iterator[Span]:
        def visit(sp: Span) -> Iterator[Span]:
            yield sp
            for child in sp.children:
                yield from visit(child)

        for root in self.roots():
            yield from visit(root)

    def stage_table(self) -> dict[str, dict]:
        buckets: dict[str, list[Span]] = {}
        for sp in self._walk():
            buckets.setdefault(sp.name, []).append(sp)

        table: dict[str, dict] = {}
        for name, spans in buckets.items():
            durations = [s.duration_s for s in spans]
            table[name] = {
                "count": len(spans),
                "total_s": sum(durations),
                "self_s": sum(s.self_time_s for s in spans),
                "p95_s": percentile(durations, 95),
            }
        return table

    def report(self) -> dict:
        return {"stages": self.stage_table(), "counters": self.counters()}

    def reset(self) -> None:
        with self._lock:
            self._roots.clear()
            self._counters.clear()
            self._samples.clear()
        self._local.stack = []
