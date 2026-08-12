"""Phoenix / OpenTelemetry instrumentation."""

from .phoenix_setup import get_tracer, redact_value, register_tracing

__all__ = ["get_tracer", "redact_value", "register_tracing"]
