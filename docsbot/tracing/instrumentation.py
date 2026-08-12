"""Span helpers for ingest / agent / tools.

Each helper starts a span (or no-ops if tracing unset), sets redacted
attributes, and yields a context manager. Tests assert attribute keys exist
on a recording exporter.

Every attribute passes through `redact_value` on its way in — the ONE choke
point where secrets get scrubbed. Instrumented call sites stay honest by
construction instead of by review.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .phoenix_setup import get_tracer, redact_value


@contextmanager
def _span(name: str, attrs: dict[str, Any]) -> Iterator[Any]:
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as span:
        if span is not None and hasattr(span, "set_attribute"):
            for key, value in attrs.items():
                span.set_attribute(key, redact_value(key, value))
        yield span


@contextmanager
def span_ingest(source_path: str, content_hash: str) -> Iterator[Any]:
    with _span(
        "docsbot.ingest",
        {"ingest.source_path": source_path, "ingest.content_hash": content_hash},
    ) as span:
        yield span


@contextmanager
def span_agent_turn(session_id: str, question: str) -> Iterator[Any]:
    with _span(
        "docsbot.agent_turn",
        {"agent.session_id": session_id, "agent.question": question},
    ) as span:
        yield span


@contextmanager
def span_tool(tool_name: str, hop_index: int, **attrs: Any) -> Iterator[Any]:
    with _span(
        "docsbot.tool",
        {"tool.name": tool_name, "tool.hop_index": hop_index, **attrs},
    ) as span:
        yield span


@contextmanager
def span_llm(model: str, **attrs: Any) -> Iterator[Any]:
    with _span("docsbot.llm", {"llm.model": model, **attrs}) as span:
        yield span
