"""Wire Phoenix (OpenTelemetry) tracing.

Real wiring:

    from phoenix.otel import register
    register(project_name=..., endpoint=..., auto_instrument=False)

Tests inject a fake TracerProvider / exporter — do not require a live Phoenix
for `pytest -m phase17`.

Redaction: any attribute key matching secret patterns (api_key, password,
authorization, token) must be replaced with ``[REDACTED]`` before set_attribute.

Design note: the module holds ONE provider global. Injection (tests) and real
Phoenix wiring go through the same `register_tracing`, so instrumented code
never knows which world it's in — that's what makes the spans testable.
"""

from __future__ import annotations

import re
from typing import Any

from docsbot.config import settings

_SECRET_KEY = re.compile(r"(api[_-]?key|password|authorization|secret|token)", re.I)

_MAX_ATTR_LEN = 1000  # keep traces readable; a 50 KB prompt is not an attribute

_provider: Any | None = None


class _NoopSpan:
    """Absorbs attribute writes when tracing was never registered."""

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: ARG002
        pass

    def __enter__(self) -> "_NoopSpan":
        return self

    def __exit__(self, *exc: object) -> None:
        pass


class _NoopTracer:
    def start_as_current_span(self, name: str, **kwargs: Any) -> _NoopSpan:  # noqa: ARG002
        return _NoopSpan()


def register_tracing(
    *,
    project_name: str = "docsbot-kg",
    endpoint: str | None = None,
    tracer_provider: Any | None = None,
) -> Any:
    """Configure OTel export to Phoenix. Return the TracerProvider used."""
    global _provider
    if tracer_provider is not None:  # injected (tests, or an app-managed provider)
        _provider = tracer_provider
        return _provider
    try:
        from phoenix.otel import register  # heavy optional dep: [obs] extra

        _provider = register(
            project_name=project_name or settings.phoenix_project,
            endpoint=endpoint or settings.phoenix_collector_endpoint,
            auto_instrument=False,
        )
    except ImportError:
        # No Phoenix installed: fall back to a plain SDK provider so spans
        # still exist (exportable later), or to a no-op if even OTel is absent.
        try:
            from opentelemetry.sdk.trace import TracerProvider

            _provider = TracerProvider()
        except ImportError:
            _provider = None
    return _provider


def get_tracer(name: str = "docsbot") -> Any:
    if _provider is None:
        return _NoopTracer()
    return _provider.get_tracer(name)


def redact_value(key: str, value: Any) -> Any:
    """If key looks secret, return '[REDACTED]'; else value (truncated if huge)."""
    if _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, str) and len(value) > _MAX_ATTR_LEN:
        return value[:_MAX_ATTR_LEN] + f"…[+{len(value) - _MAX_ATTR_LEN} chars]"
    return value


def secret_key_pattern() -> re.Pattern[str]:
    return _SECRET_KEY
