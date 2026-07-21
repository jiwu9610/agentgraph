"""The CLI entrypoint.

Run after `pip install -e .`:
    docsbot ask "what is RAG?"          # grounded answer over docs/
    docsbot chat                        # interactive streaming chat
    docsbot count "some text"           # token counting (cost awareness)
"""

from __future__ import annotations

import sys
from pathlib import Path

from .chat import Conversation
from .client import get_client
from .config import settings

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"


def cmd_chat() -> None:
    """Interactive, streaming, multi-turn chat."""
    convo = Conversation()
    print("DocsBot chat. Ctrl-C to quit.\n")
    try:
        while True:
            user = input("you > ").strip()
            if not user:
                continue
            print("bot > ", end="", flush=True)
            for piece in convo.stream(user):
                print(piece, end="", flush=True)
            print("\n")
    except (KeyboardInterrupt, EOFError):
        print("\nbye!")


def cmd_ask(question: str) -> None:
    """One-shot RAG question over the docs/ corpus."""
    from .rag import DocsBot  # imported lazily so `chat` works without the RAG stack

    bot = DocsBot(DOCS_DIR)
    answer = bot.ask(question)
    print(answer.text)
    print("\n--- retrieved from ---")
    for hit in answer.hits:
        print(f"  {hit.chunk.source} #{hit.chunk.index}  (score {hit.score:.3f})")


def cmd_count(text: str) -> None:
    """Count tokens. Tokens are the unit of cost AND of context limits."""
    n = get_client().models.count_tokens(model=settings.chat_model, contents=text)
    print(f"{n.total_tokens} tokens")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd, *rest = args
    if cmd == "chat":
        cmd_chat()
    elif cmd == "ask":
        cmd_ask(" ".join(rest))
    elif cmd == "count":
        cmd_count(" ".join(rest))
    else:
        print(f"unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
