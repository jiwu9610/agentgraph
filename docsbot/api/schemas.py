"""The API contract, as Pydantic models.

These models are the shared contract between the backend and the frontend:
they drive FastAPI's request validation, response serialization, and the
generated OpenAPI docs. The frontend's `src/types.ts` mirrors them exactly —
a field renamed here must be renamed there too.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class HealthResponse(BaseModel):
    status: str = "ok"
    chunks: int = Field(description="How many chunks are currently indexed.")


class Citation(BaseModel):
    source: str
    index: int
    score: float
    snippet: str = Field(
        default="",
        description="The retrieved chunk's text, so clients can show the evidence.",
    )


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str
    citations: list[Citation]
    cost_usd: float = Field(
        default=0.0, description="What this request's model usage cost."
    )


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = Field(
        default=None,
        description="Omit on the first turn; the server returns one to send back next time.",
    )


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    cost_usd: float = Field(
        default=0.0, description="What this request's model usage cost."
    )
