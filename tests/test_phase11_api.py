"""API specs — the FastAPI service.

No network and no corpus: we override the `get_bot` dependency with a FAKE bot
that returns a canned answer, so these tests exercise the routing, auth, and
SSE streaming — not Gemini.

    pip install -e ".[dev,api]"
    pytest -m phase11
"""

import json

import pytest

pytestmark = [pytest.mark.phase2, pytest.mark.phase11]

pytest.importorskip("fastapi", reason="run: pip install -e '.[dev,api]'")

from fastapi.testclient import TestClient

from docsbot.api import app as app_module
from docsbot.config import settings
from docsbot.ingest import Chunk
from docsbot.rag import Answer
from docsbot.store import Hit


class FakeStore:
    def __len__(self):
        return 3


class FakeBot:
    """Stand-in for rag.DocsBot: fixed answer + citations, no model calls."""

    def __init__(self):
        self.store = FakeStore()

    def ask(self, question):
        return Answer(
            text="RAG grounds answers in retrieved text. (rag_overview.md)",
            hits=[
                Hit(Chunk("RAG grounds answers...", "rag_overview.md", 0), score=0.91),
                Hit(Chunk("embeddings are vectors...", "embeddings.md", 2), score=0.55),
            ],
        )


@pytest.fixture
def client():
    app = app_module.create_app()
    app.dependency_overrides[app_module.get_bot] = lambda: FakeBot()
    return TestClient(app)


def _token(client):
    r = client.post(
        "/auth/login",
        json={"username": settings.auth_user, "password": settings.auth_password},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def _auth(client):
    return {"Authorization": f"Bearer {_token(client)}"}


# --- health (open) ----------------------------------------------------------

def test_health_is_open_and_reports_chunk_count(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "chunks": 3}


# --- auth -------------------------------------------------------------------

def test_login_rejects_bad_credentials(client):
    r = client.post("/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_protected_route_requires_a_token(client):
    r = client.post("/ask", json={"question": "what is RAG?"})
    assert r.status_code == 401  # no Authorization header


def test_protected_route_rejects_a_garbage_token(client):
    r = client.post(
        "/ask",
        json={"question": "what is RAG?"},
        headers={"Authorization": "Bearer not-a-real-jwt"},
    )
    assert r.status_code == 401


# --- /ask -------------------------------------------------------------------

def test_ask_returns_answer_and_citations(client):
    r = client.post("/ask", json={"question": "what is RAG?"}, headers=_auth(client))
    assert r.status_code == 200
    body = r.json()
    assert "grounds answers" in body["answer"]
    sources = {c["source"] for c in body["citations"]}
    assert "rag_overview.md" in sources
    assert {"source", "index", "score"} <= set(body["citations"][0].keys())


# --- /ask/stream (SSE) ------------------------------------------------------

def _parse_sse(text):
    """Pull the JSON payloads out of an SSE body ('data: {...}\\n\\n')."""
    events = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:"):].strip()))
    return events


def test_ask_stream_emits_tokens_then_citations_then_done(client):
    r = client.post(
        "/ask/stream", json={"question": "what is RAG?"}, headers=_auth(client)
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(r.text)
    types = [e["type"] for e in events]

    assert types[-1] == "done"                      # terminates with done
    assert "citations" in types                     # citations event present
    assert types.index("citations") < types.index("done")
    # the streamed token texts, concatenated, reconstruct the answer
    streamed = "".join(e["text"] for e in events if e["type"] == "token")
    assert "grounds answers" in streamed


# --- /chat (sessions) -------------------------------------------------------

def test_chat_issues_a_session_id_and_remembers_it(client):
    headers = _auth(client)
    r1 = client.post("/chat", json={"message": "hi"}, headers=headers)
    assert r1.status_code == 200
    sid = r1.json()["session_id"]
    assert sid

    # Sending the same session_id back is accepted (continues the conversation).
    r2 = client.post(
        "/chat", json={"message": "again", "session_id": sid}, headers=headers
    )
    assert r2.status_code == 200
    assert r2.json()["session_id"] == sid
