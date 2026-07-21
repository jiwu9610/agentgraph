"""Multi-turn conversation + streaming.

Key idea: the API is STATELESS. The model does not remember anything between
calls. A "conversation" is just the full history re-sent every turn. The SDK's
`chats` helper keeps that history, doing exactly what manual management would:
appending each turn to a list.

    history grows every turn:
      [user: "my name is Sam"]
      [user: "my name is Sam", model: "Hi Sam!", user: "what's my name?"]  <- model can answer
"""

from __future__ import annotations

from collections.abc import Iterator

from google.genai import types

from .client import get_client
from .config import settings

DEFAULT_SYSTEM = (
    "You are DocsBot, a concise and friendly assistant. "
    "Answer in plain language. If you don't know, say so."
)


class Conversation:
    """A multi-turn chat session. Holds history so the model has context."""

    def __init__(self, system: str = DEFAULT_SYSTEM) -> None:
        self._chat = get_client().chats.create(
            model=settings.chat_model,
            config=types.GenerateContentConfig(system_instruction=system),
        )

    def send(self, message: str) -> str:
        """Send one turn, return the full reply text."""
        return self._chat.send_message(message).text or ""

    def stream(self, message: str) -> Iterator[str]:
        """Send one turn, yield text chunks as they arrive.

        Streaming matters for UX: the user sees words appear instead of staring
        at a spinner.
        """
        for chunk in self._chat.send_message_stream(message):
            if chunk.text:
                yield chunk.text
