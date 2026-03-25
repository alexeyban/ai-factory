"""Optional OpenTelemetry tracing helpers for AI Factory.

If opentelemetry-api / sdk are not installed the module degrades to a no-op
tracer so the rest of the codebase never has to guard against import errors.

Usage::

    from shared.tracing import get_tracer, configure_tracing

    # Call once at process startup (reads OTEL_* env vars):
    configure_tracing()

    tracer = get_tracer(__name__)
    with tracer.start_as_current_span("my.operation") as span:
        span.set_attribute("key", "value")
        ...
"""
from __future__ import annotations

import logging
import os

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Try to import the real OTel SDK; fall back to no-ops if absent
# ---------------------------------------------------------------------------

try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.resources import Resource

    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False

try:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    _OTLP_AVAILABLE = True
except ImportError:
    _OTLP_AVAILABLE = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def configure_tracing(service_name: str | None = None) -> None:
    """Set up the global TracerProvider once at process startup.

    Reads:
        OTEL_SERVICE_NAME          (default: ai-factory)
        OTEL_EXPORTER_OTLP_ENDPOINT (default: http://otel-collector:4317)
        OTEL_TRACES_EXPORTER       (default: otlp; set to 'none' to disable)
    """
    if not _OTEL_AVAILABLE:
        LOGGER.debug("[tracing] opentelemetry-sdk not installed — tracing disabled")
        return

    exporter_type = os.getenv("OTEL_TRACES_EXPORTER", "otlp").lower()
    if exporter_type == "none":
        return

    svc = service_name or os.getenv("OTEL_SERVICE_NAME", "ai-factory")
    resource = Resource.create({"service.name": svc})
    provider = TracerProvider(resource=resource)

    if exporter_type == "otlp" and _OTLP_AVAILABLE:
        endpoint = os.getenv(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317"
        )
        exporter = OTLPSpanExporter(endpoint=endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        LOGGER.info("[tracing] OTLP exporter configured → %s", endpoint)
    else:
        LOGGER.debug(
            "[tracing] OTLP exporter not available (exporter_type=%s, otlp_available=%s)",
            exporter_type,
            _OTLP_AVAILABLE,
        )

    trace.set_tracer_provider(provider)


def get_tracer(name: str):
    """Return a tracer (real or no-op)."""
    if not _OTEL_AVAILABLE:
        return _NoOpTracer()
    from opentelemetry import trace
    return trace.get_tracer(name)


# ---------------------------------------------------------------------------
# No-op fallback implementation
# ---------------------------------------------------------------------------

class _NoOpSpan:
    def set_attribute(self, key: str, value) -> None:
        pass

    def record_exception(self, exc: Exception) -> None:
        pass

    def set_status(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _NoOpTracer:
    def start_as_current_span(self, name: str, **kwargs):
        return _NoOpSpan()

    def start_span(self, name: str, **kwargs):
        return _NoOpSpan()
