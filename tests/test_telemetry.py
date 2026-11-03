"""Telemetry is env-gated: absent endpoint = untouched process, present = tenant-tagged spans (§3)."""

import pytest
from django.test import Client
from django_tenants.utils import tenant_context
from opentelemetry import trace
from opentelemetry.instrumentation.django import DjangoInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from config import telemetry
from services import content as svc
from tenants.models import Domain, Tenant


def test_disabled_without_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert telemetry.configure_telemetry() is False


@pytest.mark.django_db(transaction=True)
def test_request_span_carries_tenant_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    # Capture spans in memory instead of exporting over the network.
    exporter = InMemorySpanExporter()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid:4318")
    monkeypatch.setattr(telemetry, "_build_span_exporter", lambda: exporter)
    monkeypatch.setattr(telemetry, "_configured", False)  # restored on teardown

    assert telemetry.configure_telemetry() is True
    try:
        tenant = Tenant.objects.create(schema_name="otel_site", name="Otel")
        Domain.objects.create(domain="otel.localhost", tenant=tenant, is_primary=True)
        with tenant_context(tenant):
            root = svc.create_element("text", content="traced")
            svc.create_page(slug=svc.HOME_SLUG, name="Home", root_element=root)

        response = Client().get("/", headers={"host": "otel.localhost"})
        assert response.status_code == 200
        assert "traced" in response.content.decode()

        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)  # the SDK provider config.telemetry installs
        provider.force_flush()
        tagged = {span.attributes.get("uruk.tenant") for span in exporter.get_finished_spans() if span.attributes}
        assert "otel_site" in tagged
    finally:
        # Global patches must not leak into the rest of the suite.
        DjangoInstrumentor().uninstrument()
        PsycopgInstrumentor().uninstrument()
        RedisInstrumentor().uninstrument()
        LoggingInstrumentor().uninstrument()
