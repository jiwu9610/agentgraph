"""API extension specs: rate limiting, chat sessions, cost accounting, snippets.

Everything runs against fakes — no network, no real Redis, no model calls:

- Redis is replaced by a small in-memory fake (or one that always raises, for
  the fail-open cases) injected into the limiter / session store.
- The RAG bot is replaced via the ``get_bot`` dependency override, as in the
  phase 11 API tests; rag-level cost recording is tested by monkeypatching the
  provider client.
"""

from __future__ import annotations

import io
import json
import re
import sys
import uuid

import pytest

pytest.importorskip("fastapi", reason="run: pip install -e '.[dev,api]'")

from fastapi.testclient import TestClient

from docsbot.api import app as app_module
from docsbot.api.rate_limit import RateLimiter
from docsbot.api.sessions import RedisSessionStore
from docsbot.config import settings
from docsbot.ingest import Chunk
from docsbot.observability import UsageLedger
from docsbot.rag import RAG_SYSTEM, Answer, DocsBot
from docsbot.store import Hit


# --- fakes -------------------------------------------------------------------


class FakeRedis:
    """Just enough of the redis client surface for the limiter + session store."""

    def __init__(self):
        self.counters: dict[str, int] = {}
        self.lists: dict[str, list] = {}
        self.ttls: dict[str, int] = {}

    def incr(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def lrange(self, key, start, end):
        items = self.lists.get(key, [])
        stop = len(items) if end == -1 else end + 1
        return items[start:stop]


class ExplodingRedis:
    """Every operation raises, like a client whose server is unreachable."""

    def __getattr__(self, name):
        def boom(*args, **kwargs):
            raise ConnectionError("connection refused")

        return boom


class FakeStore:
    def __len__(self):
        return 3


class FakeBot:
    """Canned answers; records every question so tests can inspect prompts."""

    def __init__(self):
        self.store = FakeStore()
        self.questions: list[str] = []

    def ask(self, question):
        self.questions.append(question)
        n = len(self.questions)
        return Answer(
            text=f"Reply number {n}. (guide.md #0)",
            hits=[Hit(Chunk("Retrieval grounds answers in text.", "guide.md", 0), score=0.9)],
            input_tokens=1000,
            output_tokens=100,
            cost_usd=0.0125,
        )


@pytest.fixture
def bot():
    return FakeBot()


@pytest.fixture
def client(bot):
    """A TestClient with fakes for the bot, the limiter, and the session store."""
    app = app_module.create_app()
    app.dependency_overrides[app_module.get_bot] = lambda: bot
    limiter = RateLimiter(max_requests=100, window_seconds=60, client=FakeRedis())
    app.dependency_overrides[app_module.get_rate_limiter] = lambda: limiter
    store = RedisSessionStore(ttl_seconds=60, client=FakeRedis())
    app.dependency_overrides[app_module.get_session_store] = lambda: store
    return TestClient(app)


def _auth(client):
    r = client.post(
        "/auth/login",
        json={"username": settings.auth_user, "password": settings.auth_password},
    )
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _parse_sse(text):
    events = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:"):].strip()))
    return events


# --- rate limiting: fixed window ---------------------------------------------


def test_limiter_counts_per_key_within_window():
    fake = FakeRedis()
    limiter = RateLimiter(max_requests=2, window_seconds=60, client=fake)

    assert limiter.allow("alice") is True
    assert limiter.allow("alice") is True
    assert limiter.allow("alice") is False  # over the limit
    assert limiter.allow("bob") is True     # keys are independent

    # The window key expires so counts reset after window_seconds.
    assert 60 in fake.ttls.values()


def test_over_limit_returns_429(bot):
    app = app_module.create_app()
    app.dependency_overrides[app_module.get_bot] = lambda: bot
    limiter = RateLimiter(max_requests=2, window_seconds=60, client=FakeRedis())
    app.dependency_overrides[app_module.get_rate_limiter] = lambda: limiter
    store = RedisSessionStore(ttl_seconds=60, client=FakeRedis())
    app.dependency_overrides[app_module.get_session_store] = lambda: store
    client = TestClient(app)
    headers = _auth(client)

    assert client.post("/ask", json={"question": "q1"}, headers=headers).status_code == 200
    assert client.post("/ask", json={"question": "q2"}, headers=headers).status_code == 200
    r = client.post("/ask", json={"question": "q3"}, headers=headers)
    assert r.status_code == 429
    # /chat shares the same per-user budget.
    assert client.post("/chat", json={"message": "hi"}, headers=headers).status_code == 429


def test_unlimited_routes_are_not_rate_limited(bot):
    app = app_module.create_app()
    app.dependency_overrides[app_module.get_bot] = lambda: bot
    limiter = RateLimiter(max_requests=0, window_seconds=60, client=FakeRedis())
    app.dependency_overrides[app_module.get_rate_limiter] = lambda: limiter
    client = TestClient(app)

    assert client.get("/health").status_code == 200
    headers = _auth(client)  # login must also stay reachable
    assert client.post("/ask", json={"question": "q"}, headers=headers).status_code == 429


# --- rate limiting: fail-open ------------------------------------------------


def test_limiter_fails_open_on_redis_errors():
    limiter = RateLimiter(max_requests=0, window_seconds=60, client=ExplodingRedis())
    # Even with a zero budget, a broken Redis must admit the request.
    assert limiter.allow("alice") is True
    assert limiter.allow("alice") is True


def test_limiter_fails_open_when_redis_module_is_missing(monkeypatch):
    # `import redis` inside the limiter raises; there is no module-level name
    # to catch with, so the handler must not depend on one (no NameError).
    monkeypatch.setitem(sys.modules, "redis", None)
    limiter = RateLimiter(max_requests=0, window_seconds=60)
    assert limiter.allow("alice") is True


def test_api_stays_up_when_redis_is_down(bot):
    app = app_module.create_app()
    app.dependency_overrides[app_module.get_bot] = lambda: bot
    limiter = RateLimiter(max_requests=0, window_seconds=60, client=ExplodingRedis())
    app.dependency_overrides[app_module.get_rate_limiter] = lambda: limiter
    store = RedisSessionStore(ttl_seconds=60, client=ExplodingRedis())
    app.dependency_overrides[app_module.get_session_store] = lambda: store
    client = TestClient(app)
    headers = _auth(client)

    assert client.post("/ask", json={"question": "q"}, headers=headers).status_code == 200
    r = client.post("/chat", json={"message": "hi"}, headers=headers)
    assert r.status_code == 200  # chat degrades to stateless, it does not 500
    assert r.json()["session_id"]


# --- chat sessions -----------------------------------------------------------


def test_chat_issues_a_uuid_session_id(client):
    headers = _auth(client)
    r = client.post("/chat", json={"message": "hello"}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    uuid.UUID(body["session_id"])  # server-issued id is a valid uuid
    assert body["reply"].startswith("Reply number 1.")


def test_chat_continues_the_conversation_with_history(client, bot):
    headers = _auth(client)
    r1 = client.post("/chat", json={"message": "What is retrieval?"}, headers=headers)
    sid = r1.json()["session_id"]
    first_reply = r1.json()["reply"]

    r2 = client.post(
        "/chat", json={"message": "Tell me more", "session_id": sid}, headers=headers
    )
    assert r2.status_code == 200
    assert r2.json()["session_id"] == sid

    # The second question the bot saw carries the prior turns.
    assert len(bot.questions) == 2
    assert "What is retrieval?" in bot.questions[1]
    assert first_reply in bot.questions[1]
    assert "Tell me more" in bot.questions[1]
    # The first question was sent bare — no empty transcript wrapper.
    assert bot.questions[0] == "What is retrieval?"


def test_chat_stream_emits_session_tokens_citations_cost_done(client):
    headers = _auth(client)
    r = client.post("/chat/stream", json={"message": "hello"}, headers=headers)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(r.text)
    types = [e["type"] for e in events]

    # The session id arrives first so a client can bind it immediately.
    assert types[0] == "session"
    uuid.UUID(events[0]["session_id"])

    streamed = "".join(e["text"] for e in events if e["type"] == "token")
    assert streamed.startswith("Reply number 1.")

    assert types[-1] == "done"
    assert types.index("citations") > max(i for i, t in enumerate(types) if t == "token")
    assert types.index("cost") > types.index("citations")
    cost_event = events[types.index("cost")]
    assert cost_event["cost_usd"] == pytest.approx(0.0125)


def test_chat_stream_get_continues_an_existing_session(client, bot):
    headers = _auth(client)
    r1 = client.post("/chat/stream", json={"message": "first message"}, headers=headers)
    sid = next(e["session_id"] for e in _parse_sse(r1.text) if e["type"] == "session")

    r2 = client.get(
        "/chat/stream",
        params={"message": "second message", "session_id": sid},
        headers=headers,
    )
    assert r2.status_code == 200
    events = _parse_sse(r2.text)
    assert next(e["session_id"] for e in events if e["type"] == "session") == sid

    assert len(bot.questions) == 2
    assert "first message" in bot.questions[1]
    assert "second message" in bot.questions[1]


def test_session_store_round_trips_turns_and_sets_ttl():
    fake = FakeRedis()
    store = RedisSessionStore(ttl_seconds=120, client=fake)
    store.append("sid-1", "user", "hello")
    store.append("sid-1", "assistant", "hi there")

    assert store.history("sid-1") == [
        {"role": "user", "text": "hello"},
        {"role": "assistant", "text": "hi there"},
    ]
    assert store.history("sid-other") == []
    assert 120 in fake.ttls.values()


def test_session_store_degrades_on_redis_errors():
    store = RedisSessionStore(ttl_seconds=60, client=ExplodingRedis())
    store.append("sid", "user", "hello")  # must not raise
    assert store.history("sid") == []


# --- cost accounting ---------------------------------------------------------


class _FakeUsage:
    def __init__(self, prompt, candidates):
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates


class _FakeResponse:
    def __init__(self, text, usage):
        self.text = text
        self.usage_metadata = usage


class _FakeModels:
    def __init__(self, response):
        self._response = response

    def generate_content(self, **kwargs):
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.models = _FakeModels(response)


class _HitStore:
    def __init__(self, hits):
        self._hits = hits

    def search(self, query, k=None):
        return self._hits

    def __len__(self):
        return len(self._hits)


def _bare_bot(hits) -> DocsBot:
    """A DocsBot without corpus embedding: fake store, fresh ledger."""
    bot = DocsBot.__new__(DocsBot)
    bot.store = _HitStore(hits)
    bot.ledger = UsageLedger()
    return bot


def test_ask_records_usage_in_the_ledger_and_prices_the_request(monkeypatch):
    hits = [Hit(Chunk("some context", "guide.md", 0), score=0.8)]
    bot = _bare_bot(hits)
    response = _FakeResponse("An answer. (guide.md #0)", _FakeUsage(1_000_000, 1_000_000))
    monkeypatch.setattr("docsbot.rag.get_client", lambda: _FakeClient(response))

    answer = bot.ask("what?")

    assert bot.ledger.calls == 1
    assert bot.ledger.input_tokens == 1_000_000
    assert bot.ledger.output_tokens == 1_000_000
    expected = settings.usd_per_1m_input + settings.usd_per_1m_output
    assert answer.cost_usd == pytest.approx(expected)
    assert answer.input_tokens == 1_000_000
    assert answer.output_tokens == 1_000_000


def test_ask_tolerates_missing_usage_counts(monkeypatch):
    hits = [Hit(Chunk("some context", "guide.md", 0), score=0.8)]
    bot = _bare_bot(hits)
    response = _FakeResponse("An answer.", _FakeUsage(500, None))
    monkeypatch.setattr("docsbot.rag.get_client", lambda: _FakeClient(response))

    answer = bot.ask("what?")

    assert bot.ledger.input_tokens == 500
    assert bot.ledger.output_tokens == 0
    assert answer.cost_usd == pytest.approx(500 / 1e6 * settings.usd_per_1m_input)


def test_empty_store_records_nothing(monkeypatch):
    bot = _bare_bot([])
    monkeypatch.setattr(
        "docsbot.rag.get_client",
        lambda: pytest.fail("empty store must not call the model"),
    )

    answer = bot.ask("what?")

    assert bot.ledger.calls == 0
    assert answer.cost_usd == 0.0
    assert answer.hits == []


def test_ask_response_includes_per_request_cost(client):
    headers = _auth(client)
    r = client.post("/ask", json={"question": "q"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["cost_usd"] == pytest.approx(0.0125)


def test_chat_response_includes_per_request_cost(client):
    headers = _auth(client)
    r = client.post("/chat", json={"message": "hi"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["cost_usd"] == pytest.approx(0.0125)


def test_ask_stream_emits_a_cost_event_after_the_answer(client):
    headers = _auth(client)
    r = client.post("/ask/stream", json={"question": "q"}, headers=headers)
    assert r.status_code == 200

    events = _parse_sse(r.text)
    types = [e["type"] for e in events]
    assert "cost" in types
    assert types.index("cost") > max(i for i, t in enumerate(types) if t == "token")
    assert types[-1] == "done"
    assert events[types.index("cost")]["cost_usd"] == pytest.approx(0.0125)


# --- citations with snippets -------------------------------------------------


def test_citations_carry_the_chunk_text_as_snippet(client):
    headers = _auth(client)
    r = client.post("/ask", json={"question": "q"}, headers=headers)
    citation = r.json()["citations"][0]
    assert citation["source"] == "guide.md"
    assert citation["index"] == 0
    assert citation["snippet"] == "Retrieval grounds answers in text."


def test_stream_citations_carry_snippets(client):
    headers = _auth(client)
    r = client.post("/ask/stream", json={"question": "q"}, headers=headers)
    events = _parse_sse(r.text)
    citations = next(e["citations"] for e in events if e["type"] == "citations")
    assert citations[0]["snippet"] == "Retrieval grounds answers in text."


def test_system_prompt_pins_the_inline_citation_format():
    assert "(file.md #N)" in RAG_SYSTEM
    # ...and shows at least one concrete example of the format.
    assert re.search(r"\([a-z_]+\.md #\d+\)", RAG_SYSTEM)
