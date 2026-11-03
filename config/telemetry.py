"""Env-gated OpenTelemetry setup (ARCHITECTURE.md §3).

Observability follows the same rule as every other backend choice: the environment
decides, and nothing loads unless configured. The gate is the standard
``OTEL_EXPORTER_OTLP_ENDPOINT`` variable — when it is unset,
:func:`configure_telemetry` returns without importing the SDK, so a plain
deployment pays zero overhead. When set, it wires the OTel SDK plus
Django/psycopg/redis/logging instrumentation, and every request span carries the
resolved tenant ``schema_name`` (the ``uruk.tenant`` attribute).

All other knobs (service name, headers, sampling, resource attributes) are the
standard ``OTEL_*`` variables, read by the SDK itself — nothing is renamed to
``URUK_*``. Call :func:`configure_telemetry` once per process, before the Django
application is built; ``config.wsgi`` and ``manage.py`` both do.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse
    from opentelemetry.sdk.trace.export import SpanExporter
    from opentelemetry.trace import Span

#: Process-wide guard so repeated calls (e.g. ``manage.py runserver`` importing the
#: WSGI app after already configuring) don't stack duplicate instrumentation.
_configured = False


def _tag_tenant(span: Span, request: HttpRequest, response: HttpResponse) -> None:  # noqa: ARG001 - the hook signature is OTel's
    """Stamp the resolved tenant schema on the request span.

    Runs as the Django instrumentation's response hook — by then the tenant
    middleware has resolved ``request.tenant``, so traces are filterable per
    tenant (ARCHITECTURE.md §3).
    """
    tenant = getattr(request, "tenant", None)
    if tenant is not None and span.is_recording():
        span.set_attribute("uruk.tenant", tenant.schema_name)


def _build_span_exporter() -> SpanExporter:
    """Build the OTLP-over-HTTP span exporter (endpoint/headers come from ``OTEL_*`` env)."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: PLC0415 - see configure_telemetry

    return OTLPSpanExporter()


def configure_telemetry() -> bool:
    """Initialise OpenTelemetry iff ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set; return whether it is active.

    Idempotent per process. Safe to call before ``django.setup()`` — the Django
    instrumentor inserts its middleware when settings load.
    """
    global _configured  # noqa: PLW0603 - process-wide once-only init flag
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    if _configured:
        return True

    # Imports live here, not at module top: the SDK must not load (or cost anything)
    # unless an exporter endpoint is configured — the whole point of the gate.
    from opentelemetry import trace  # noqa: PLC0415
    from opentelemetry.instrumentation.django import DjangoInstrumentor  # noqa: PLC0415
    from opentelemetry.instrumentation.logging import LoggingInstrumentor  # noqa: PLC0415
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor  # noqa: PLC0415
    from opentelemetry.instrumentation.redis import RedisInstrumentor  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

    os.environ.setdefault("OTEL_SERVICE_NAME", "uruk")
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(_build_span_exporter()))
    trace.set_tracer_provider(provider)

    DjangoInstrumentor().instrument(response_hook=_tag_tenant)
    PsycopgInstrumentor().instrument()
    RedisInstrumentor().instrument()  # no-op unless the Redis cache backend is active
    LoggingInstrumentor().instrument()  # trace/span ids onto log records, for correlation

    _configured = True
    return True
