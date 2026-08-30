"""Retrieval-augmented question answering over the local document corpus.

The pipeline: embed the question, retrieve the top-k most similar chunks from
the vector store, and ask the chat model to answer strictly from that context,
citing sources inline. Grounding the model in retrieved text is what lets it
answer about private documents — and what lets it refuse when the documents
don't contain the answer, instead of guessing.

Every model call's token usage is recorded in the bot's `UsageLedger`, and each
`Answer` carries its own token counts and dollar cost so callers can surface
per-request spend. The empty-store path never calls the model and records
nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from google.genai import types

from .client import get_client
from .config import settings
from .ingest import load_corpus
from .observability import UsageLedger
from .store import Hit, VectorStore

# The citation format is pinned, with an example, so answers cite consistently
# enough for clients to parse and link the citations.
RAG_SYSTEM = (
    "You are DocsBot. Answer the user's question using ONLY the provided context. "
    "If the answer is not in the context, say 'I couldn't find that in the docs.' "
    "Cite sources inline as (file.md #N), where file.md is the source filename "
    "and N is the chunk index from the context — for example: (setup.md #2)."
)


@dataclass
class Answer:
    text: str
    hits: list[Hit]  # the retrieved chunks, for citations and debugging
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0  # what this one request cost


class DocsBot:
    """A document-grounded question answerer with usage accounting."""

    def __init__(self, docs_dir: str | Path) -> None:
        self.store = VectorStore()
        self.store.add(load_corpus(docs_dir))
        self.ledger = UsageLedger()

    def _build_prompt(self, question: str, hits: list[Hit]) -> str:
        context = "\n\n".join(
            f"[source: {h.chunk.source} #{h.chunk.index}]\n{h.chunk.text}" for h in hits
        )
        return f"Context:\n{context}\n\nQuestion: {question}"

    def ask(self, question: str) -> Answer:
        hits = self.store.search(question)
        if not hits:
            # No retrieval, no model call, no usage recorded.
            return Answer(text="The document store is empty.", hits=[])

        response = get_client().models.generate_content(
            model=settings.chat_model,
            contents=self._build_prompt(question, hits),
            config=types.GenerateContentConfig(system_instruction=RAG_SYSTEM),
        )

        # Provider responses may omit either count (e.g. no candidates).
        usage = getattr(response, "usage_metadata", None)
        input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        self.ledger.record(input_tokens, output_tokens)

        # Price this one request with the same arithmetic the ledger uses.
        request = UsageLedger()
        request.record(input_tokens, output_tokens)

        return Answer(
            text=response.text or "",
            hits=hits,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=request.cost_usd,
        )
