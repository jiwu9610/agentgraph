"""Tool use (a.k.a. function calling).

On its own the model can only talk. Tools let it ACT: hand it Python functions,
it decides when to call them, the calling code runs them, it uses the results.
This is the seed of every "agent."

The loop, conceptually:

    user asks ──▶ model: "call get_word_count('...')" ──▶ your code runs it
        ▲                                                        │
        └──────────  model reads the result, answers  ◀──────────┘

Gemini's SDK can run that loop FOR you (automatic function calling): just pass
plain Python functions as `tools`. The function's docstring and type hints
become the schema the model sees — so write them well.
"""

from __future__ import annotations

from google.genai import types

from .client import get_client
from .config import settings


def word_count(text: str) -> int:
    """Count the number of words in a piece of text.

    Args:
        text: The text to count words in.
    """
    return len(text.split())


def reverse_text(text: str) -> str:
    """Reverse a string character by character.

    Args:
        text: The text to reverse.
    """
    return text[::-1]


# The tools we expose to the model.
TOOLS = [word_count, reverse_text]


def run_with_tools(prompt: str) -> str:
    """Answer a prompt, letting the model call our tools as needed.

    Automatic function calling: the SDK executes TOOLS and feeds results back
    until the model produces a final answer. To see the loop yourself, set
    `automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)`
    and handle the `function_call` parts by hand.
    """
    response = get_client().models.generate_content(
        model=settings.chat_model,
        contents=prompt,
        config=types.GenerateContentConfig(tools=TOOLS),
    )
    return response.text or ""
