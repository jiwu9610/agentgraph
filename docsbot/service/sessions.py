"""Conversation session storage.

Holds each conversation's transcript so `/chat` can be multi-turn.

Operationally this is the kind of module that pages someone at 4am. In the
instrumented baseline scenario it worked perfectly in every test and for the
first several hours of traffic — then the pod's memory climbed past its limit,
the orchestrator killed it, users saw a blip, memory started climbing again,
and the graph looked like a sawtooth. The TTL purge and hard cap below are
what make the sawtooth flat.

That incident is INC-004; incident-by-incident analysis: PART4_POSTMORTEMS.md.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class Session:
    session_id: str
    turns: list[tuple[str, str]] = field(default_factory=list)  # (user, assistant)
    created_at: float = field(default_factory=time.monotonic)
    last_seen_at: float = field(default_factory=time.monotonic)


class SessionStore:
    """In-process conversation store.

    Configured with a TTL and a cap — and enforces both. Being *configured*
    with a limit and *enforcing* one are different things.
    """

    def __init__(self, ttl_s: float = 3600.0, max_sessions: int = 1000) -> None:
        self.ttl_s = ttl_s
        self.max_sessions = max_sessions
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str) -> Session:
        self.purge_expired()
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                sess = Session(session_id=session_id)
                self._sessions[session_id] = sess
            sess.last_seen_at = time.monotonic()
        # Enforce AFTER inserting, or the cap is always off by one.
        self._enforce_cap()
        return sess

    def append(self, session_id: str, user_msg: str, assistant_msg: str) -> None:
        self.purge_expired()
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                sess = Session(session_id=session_id)
                self._sessions[session_id] = sess
            sess.turns.append((user_msg, assistant_msg))
            sess.last_seen_at = time.monotonic()
        self._enforce_cap()

    def history(self, session_id: str) -> list[tuple[str, str]]:
        with self._lock:
            sess = self._sessions.get(session_id)
            return list(sess.turns) if sess else []

    def purge_expired(self, *, now: float | None = None) -> int:
        """Drop sessions idle for longer than the TTL. Returns how many went.

        Called on every write, so idle conversations cannot accumulate.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            dead = [sid for sid, s in self._sessions.items()
                    if now - s.last_seen_at > self.ttl_s]
            for sid in dead:
                self._sessions.pop(sid, None)
        return len(dead)

    def _enforce_cap(self) -> None:
        """Hard ceiling on session count: evict least-recently-seen first.

        TTL alone is not enough. A burst of 100k distinct session ids inside the
        TTL window will still OOM you, and an attacker (or a retry loop with a
        fresh uuid each time) can produce exactly that.
        """
        with self._lock:
            overflow = len(self._sessions) - self.max_sessions
            if overflow <= 0:
                return
            victims = sorted(self._sessions.items(),
                             key=lambda kv: kv[1].last_seen_at)[:overflow]
            for sid, _ in victims:
                self._sessions.pop(sid, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def stats(self) -> dict:
        with self._lock:
            turns = sum(len(s.turns) for s in self._sessions.values())
            return {"sessions": len(self._sessions), "total_turns": turns}
