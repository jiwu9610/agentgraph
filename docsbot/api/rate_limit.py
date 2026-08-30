"""Redis-backed fixed-window rate limiting.

Requests are counted per key (the authenticated username) in fixed windows of
``window_seconds``: each request INCRs a key scoped to the current window, the
first increment sets the key to expire with the window, and a count above
``max_requests`` refuses the request.

Fail-open invariant
-------------------
The limiter is an anti-abuse measure, not a load-bearing dependency: any Redis
failure — server unreachable, client library missing, protocol error — must
admit the request rather than take the API down. The exception types the
handler catches are resolved and stored on the instance at construction time,
so the ``except`` clause cannot itself fail with a NameError when the lazily
imported ``redis`` module never became available.
"""

from __future__ import annotations

import time

from ..config import settings


class RateLimiter:
    """Fixed-window per-key request limiter backed by Redis.

    `client` is injectable for tests (anything with `incr`/`expire`); left as
    None, a real client is created lazily from `redis_url` on first use, so
    constructing the limiter never touches the network and never imports the
    optional ``redis`` dependency.
    """

    def __init__(
        self,
        max_requests: int | None = None,
        window_seconds: int | None = None,
        redis_url: str | None = None,
        client: object | None = None,
    ) -> None:
        self.max_requests = (
            settings.rate_limit_max_requests if max_requests is None else max_requests
        )
        self.window_seconds = (
            settings.rate_limit_window_seconds if window_seconds is None else window_seconds
        )
        self.redis_url = redis_url or settings.redis_url
        self._client = client
        # Resolved now, not at call time: the handler in allow() must never
        # reference a name (like `redis.RedisError`) that only exists if the
        # lazy import succeeded. Broad on purpose — any failure means "let the
        # request through", so there is nothing to gain from narrowing.
        self._errors: tuple[type[BaseException], ...] = (Exception,)

    def _get_client(self):
        if self._client is None:
            import redis  # lazy: optional dependency, only needed here

            self._client = redis.Redis.from_url(self.redis_url)
        return self._client

    def allow(self, key: str) -> bool:
        """Return True if a request under `key` fits the current window's budget.

        Fail-open: any error while talking to Redis admits the request.
        """
        try:
            client = self._get_client()
            window = int(time.time() // self.window_seconds)
            redis_key = f"ratelimit:{key}:{window}"
            count = int(client.incr(redis_key))
            if count == 1:
                # Fresh window: let the counter die with it.
                client.expire(redis_key, self.window_seconds)
            return count <= self.max_requests
        except self._errors:
            return True
