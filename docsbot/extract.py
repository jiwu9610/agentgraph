"""Structured output.

Free-text answers are great for humans, useless for code. When the model's
output must flow into a program, constrain it to a schema. Define the shape
with Pydantic; Gemini fills it in and hands back a typed object.

    response_schema=Citation  ──▶  response.parsed  is a Citation instance
                                   (not a string you have to json.loads + pray)

Stop parsing prose; request typed data.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from google.genai import types

from .client import get_client
from .config import settings


class KeyPoint(BaseModel):
    """One extracted fact with a confidence the model assigns itself."""

    claim: str = Field(description="A single factual statement from the text.")
    confidence: float = Field(description="0.0-1.0 how sure the model is.")


class Summary(BaseModel):
    """Structured summary of a document."""

    title: str
    one_liner: str = Field(description="A single-sentence gist.")
    key_points: list[KeyPoint]


def summarize(text: str) -> Summary:
    """Turn arbitrary text into a validated Summary object."""
    response = get_client().models.generate_content(
        model=settings.chat_model,
        contents=f"Summarize the following document:\n\n{text}",
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=Summary,
        ),
    )
    # .parsed is already a Summary instance because we passed a Pydantic schema.
    return response.parsed  # type: ignore[return-value]
