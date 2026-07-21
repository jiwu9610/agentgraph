"""The answer cache.

An identical question asked twice should cost nothing the second time. That is
the cheapest win in any LLM product: real traffic is enormously repetitive
(FAQ-shaped questions, retries, people re-asking after a page refresh).

In the instrumented baseline scenario, the cache was wired at every call site
yet measured a 0% hit rate — invisible, because answers stayed correct at full
price. Hence the hit/miss accounting below: a cache without instrumentation
cannot be trusted to be working.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Entry:
    value: object
    stored_at: float


class AnswerCache:
    """A small TTL cache with hit/miss accounting."""

    def __init__(self, ttl_s: float = 300.0, max_entries: int = 1024) -> None:
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._data: dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(question: str, *, model: str, top_k: int) -> str:
        """Cache key. Includes the knobs that change the answer, so tuning
        `top_k` or swapping models doesn't serve stale results."""
        return f"{question.strip().lower()}|{model}|{top_k}"

    def get(self, key: str):
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.misses += 1
                return None
            age = now - entry.stored_at
            if age >= self.ttl_s:
                # Entry is older than we allow -> treat as a miss and drop it.
                self._data.pop(key, None)
                self.misses += 1
                return None
            self.hits += 1
            return entry.value

    def put(self, key: str, value: object) -> None:
        with self._lock:
            if len(self._data) >= self.max_entries:
                oldest = min(self._data, key=lambda k: self._data[k].stored_at)
                self._data.pop(oldest, None)
            self._data[key] = _Entry(value=value, stored_at=time.monotonic())

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def stats(self) -> dict:
        return {"hits": self.hits, "misses": self.misses,
                "hit_rate": self.hit_rate, "entries": len(self._data)}
