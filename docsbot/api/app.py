"""HTTP API in front of the RAG engine.

Routes
------
    GET  /health              -> {status, chunks}            (no auth)
    POST /auth/login          -> {access_token, token_type}  (no auth)
    POST /ask        (auth)   -> {answer, citations[], cost_usd}
    GET/POST /ask/stream (auth) -> text/event-stream
    POST /chat       (auth)   -> {session_id, reply, cost_usd}  (multi-turn)
    GET/POST /chat/stream (auth) -> text/event-stream          (multi-turn)

Design notes
------------
- Routes receive their collaborators via dependencies (``get_bot``,
  ``get_rate_limiter``, ``get_session_store``) and never construct them in a
  handler, so tests (and alternate deployments) can swap any of them with
  ``app.dependency_overrides``.
- Streaming uses Server-Sent Events: one ``token`` event per answer fragment,
  then ``citations``, then ``cost``, then a terminal ``done``. Chat streams
  additionally open with a ``session`` event so the client learns its session
  id before the first token. Each event is a line of ``data: {json}\\n\\n``.
  One-way server-to-client streaming over plain HTTP is all a chat answer
  needs, so SSE is used instead of a websocket; the GET variants exist because
  native EventSource clients can only issue GETs.
- The model-calling routes (``/ask*``, ``/chat*``) sit behind a per-user
  fixed-window rate limit (429 over budget). The limiter fails open: if Redis
  is unreachable the request proceeds — anti-abuse must never be the component
  that takes the API down.
- Chat history lives in a Redis-backed session store keyed by a server-issued
  uuid, so any number of workers share sessions and idle ones expire via TTL.

The app is exposed via the factory (``uvicorn docsbot.api.app:create_app
--factory``) rather than a module-level instance, so importing this module has
no side effects.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from ..rag import Answer, DocsBot
from .auth import authenticate, create_token, require_auth
from .rate_limit import RateLimiter
from .schemas import (
    AskRequest,
    AskResponse,
    ChatRequest,
    ChatResponse,
    Citation,
    HealthResponse,
    LoginRequest,
    TokenResponse,
)
from .sessions import RedisSessionStore, SessionStore

DOCS_DIR = Path(__file__).resolve().parent.parent.parent / "docs"

_bot: DocsBot | None = None
_rate_limiter: RateLimiter | None = None
_session_store: SessionStore | None = None


def get_bot() -> DocsBot:
    """Dependency returning the shared RAG bot, built once and memoized.

    Construction embeds the whole corpus, so it must happen at most once per
    process — and only when a route actually needs the bot, keeping import
    cheap and letting tests override this dependency without ever building
    the real thing.
    """
    global _bot
    if _bot is None:
        _bot = DocsBot(DOCS_DIR)
    return _bot


def get_rate_limiter() -> RateLimiter:
    """Dependency returning the shared per-user rate limiter."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter


def get_session_store() -> SessionStore:
    """Dependency returning the shared chat-session store."""
    global _session_store
    if _session_store is None:
        _session_store = RedisSessionStore()
    return _session_store


def enforce_rate_limit(
    user: str = Depends(require_auth),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> str:
    """Authenticate, then charge the request against the user's window budget.

    Auth runs first, so unauthenticated requests stay plain 401s and the
    budget is keyed by a verified username rather than anything spoofable.
    """
    if not limiter.allow(user):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    return user


def _citations(answer: Answer) -> list[Citation]:
    return [
        Citation(
            source=h.chunk.source,
            index=h.chunk.index,
            score=h.score,
            snippet=h.chunk.text,
        )
        for h in answer.hits
    ]


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _stream_answer(answer: Answer, session_id: str | None = None) -> Iterator[str]:
    """Yield SSE events: [session], tokens, citations, cost, done.

    The answer text is split on single spaces and the separators are
    re-emitted, so concatenating every token event reconstructs the text
    exactly. The cost event follows the complete answer because the request's
    cost is only meaningful once the answer is finished.
    """
    if session_id is not None:
        yield _sse({"type": "session", "session_id": session_id})
    parts = answer.text.split(" ")
    for i, part in enumerate(parts):
        text = part if i == len(parts) - 1 else part + " "
        yield _sse({"type": "token", "text": text})
    yield _sse(
        {
            "type": "citations",
            "citations": [c.model_dump() for c in _citations(answer)],
        }
    )
    yield _sse({"type": "cost", "cost_usd": answer.cost_usd})
    yield _sse({"type": "done"})


def _continue_chat(
    message: str,
    session_id: str | None,
    bot: DocsBot,
    store: SessionStore,
) -> tuple[str, Answer]:
    """Run one chat turn: fold history into the question, answer, persist.

    The first turn (no session_id) mints a fresh uuid; the caller returns it
    so the client can continue the conversation.
    """
    sid = session_id or str(uuid4())
    history = store.history(sid)

    if history:
        transcript = "\n".join(f"{t['role']}: {t['text']}" for t in history)
        question = f"Conversation so far:\n{transcript}\n\nUser: {message}"
    else:
        question = message

    answer = bot.ask(question)
    store.append(sid, "user", message)
    store.append(sid, "assistant", answer.text)
    return sid, answer


def create_app() -> FastAPI:
    """Build and return the FastAPI app with all routes registered."""
    app = FastAPI(title="DocsBot API", version="2.0.0")

    @app.get("/health", response_model=HealthResponse)
    def health(bot: DocsBot = Depends(get_bot)) -> HealthResponse:
        return HealthResponse(status="ok", chunks=len(bot.store))

    @app.post("/auth/login", response_model=TokenResponse)
    def login(req: LoginRequest) -> TokenResponse:
        if not authenticate(req.username, req.password):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        return TokenResponse(access_token=create_token(req.username))

    @app.post("/ask", response_model=AskResponse)
    def ask(
        req: AskRequest,
        bot: DocsBot = Depends(get_bot),
        user: str = Depends(enforce_rate_limit),
    ) -> AskResponse:
        answer = bot.ask(req.question)
        return AskResponse(
            answer=answer.text,
            citations=_citations(answer),
            cost_usd=answer.cost_usd,
        )

    @app.post("/ask/stream")
    def ask_stream(
        req: AskRequest,
        bot: DocsBot = Depends(get_bot),
        user: str = Depends(enforce_rate_limit),
    ) -> StreamingResponse:
        answer = bot.ask(req.question)
        return StreamingResponse(
            _stream_answer(answer), media_type="text/event-stream"
        )

    @app.get("/ask/stream")
    def ask_stream_get(
        question: str,
        bot: DocsBot = Depends(get_bot),
        user: str = Depends(enforce_rate_limit),
    ) -> StreamingResponse:
        answer = bot.ask(question)
        return StreamingResponse(
            _stream_answer(answer), media_type="text/event-stream"
        )

    @app.post("/chat", response_model=ChatResponse)
    def chat(
        req: ChatRequest,
        bot: DocsBot = Depends(get_bot),
        store: SessionStore = Depends(get_session_store),
        user: str = Depends(enforce_rate_limit),
    ) -> ChatResponse:
        sid, answer = _continue_chat(req.message, req.session_id, bot, store)
        return ChatResponse(
            session_id=sid, reply=answer.text, cost_usd=answer.cost_usd
        )

    @app.post("/chat/stream")
    def chat_stream(
        req: ChatRequest,
        bot: DocsBot = Depends(get_bot),
        store: SessionStore = Depends(get_session_store),
        user: str = Depends(enforce_rate_limit),
    ) -> StreamingResponse:
        sid, answer = _continue_chat(req.message, req.session_id, bot, store)
        return StreamingResponse(
            _stream_answer(answer, session_id=sid), media_type="text/event-stream"
        )

    @app.get("/chat/stream")
    def chat_stream_get(
        message: str,
        session_id: str | None = None,
        bot: DocsBot = Depends(get_bot),
        store: SessionStore = Depends(get_session_store),
        user: str = Depends(enforce_rate_limit),
    ) -> StreamingResponse:
        sid, answer = _continue_chat(message, session_id, bot, store)
        return StreamingResponse(
            _stream_answer(answer, session_id=sid), media_type="text/event-stream"
        )

    return app
