"""Chat session history behind a small store interface.

A session is a server-issued id mapping to an ordered list of turns
(``{"role": ..., "text": ...}``). The API depends only on the two-method
`SessionStore` protocol, so tests inject fakes and alternate backends drop in
without touching the routes.

The default backend keeps each session as a Redis list (one JSON document per
turn) with a TTL refreshed on every write, so idle conversations expire on
their own and any number of API workers share the same history.
"""

from __future__ import annotations

import json
from typing import Protocol

from ..config import settings


class SessionStore(Protocol):
    """What the chat routes need from a session backend."""

    def history(self, session_id: str) -> list[dict]:
        """All prior turns for the session, oldest first ([] if none)."""
        ...

    def append(self, session_id: str, role: str, text: str) -> None:
        """Add one turn and refresh the session's TTL."""
        ...


class RedisSessionStore:
    """Per-session turn lists in Redis, expiring after `ttl_seconds` idle.

    Degrades instead of failing: a Redis error yields empty history and drops
    the write, so chat keeps answering (statelessly) while the store is down.
    As in the rate limiter, the exception types to catch are resolved at
    construction time so the handlers cannot NameError when the lazy
    ``import redis`` itself failed.
    """

    def __init__(
        self,
        redis_url: str | None = None,
        ttl_seconds: int | None = None,
        client: object | None = None,
    ) -> None:
        self.redis_url = redis_url or settings.redis_url
        self.ttl_seconds = (
            settings.chat_session_ttl_seconds if ttl_seconds is None else ttl_seconds
        )
        self._client = client
        self._errors: tuple[type[BaseException], ...] = (Exception,)

    def _get_client(self):
        if self._client is None:
            import redis  # lazy: optional dependency, only needed here

            self._client = redis.Redis.from_url(self.redis_url)
        return self._client

    @staticmethod
    def _key(session_id: str) -> str:
        return f"chat:session:{session_id}"

    def history(self, session_id: str) -> list[dict]:
        try:
            raw = self._get_client().lrange(self._key(session_id), 0, -1)
            return [json.loads(item) for item in raw]
        except self._errors:
            return []

    def append(self, session_id: str, role: str, text: str) -> None:
        try:
            client = self._get_client()
            key = self._key(session_id)
            client.rpush(key, json.dumps({"role": role, "text": text}))
            client.expire(key, self.ttl_seconds)
        except self._errors:
            pass
