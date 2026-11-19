"""Optional OpenTelemetry setup: export guard's spans over OTLP.

guard only depends on ``opentelemetry-api`` (spans are no-ops without an SDK). Install the ``otel``
extra and set ``OTEL_EXPORTER_OTLP_ENDPOINT`` (for example ``http://jaeger:4318``) to export.
Spans carry decisions, command names and exit codes, never arguments or outputs.
"""

from __future__ import annotations

import atexit
import logging
import os

from opentelemetry import trace

logger = logging.getLogger(__name__)


def configure(service_name: str) -> bool:
    """Set up a tracer provider with an OTLP/HTTP exporter. Returns whether exporting is on."""
    if not (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")):
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning("OTEL_EXPORTER_OTLP_ENDPOINT is set but the 'otel' extra is not installed; not exporting spans")
        return False

    name = os.environ.get("OTEL_SERVICE_NAME", service_name)
    provider = TracerProvider(resource=Resource.create({"service.name": name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    atexit.register(provider.shutdown)
    return True
