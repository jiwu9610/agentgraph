"""One thin wrapper around the Gemini client so the rest of the app never has to
think about auth. `get_client()` is memoized: one client, reused everywhere.

    from google import genai          # the new SDK: pip install google-genai
    client = genai.Client(api_key=...)
    resp = client.models.generate_content(model=..., contents="hi")
    print(resp.text)
"""

from __future__ import annotations

from functools import lru_cache

from google import genai

from .config import settings


@lru_cache(maxsize=1)
def get_client() -> genai.Client:
    """Return a shared Gemini client. Cached so we don't re-auth per call."""
    return genai.Client(api_key=settings.api_key)


def ask(prompt: str) -> str:
    """The smallest possible useful function: prompt in, text out."""
    client = get_client()
    response = client.models.generate_content(
        model=settings.chat_model,
        contents=prompt,
    )
    return response.text or ""
