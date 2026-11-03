"""Tenancy: creating a tenant provisions an isolated Postgres schema."""

import uuid

import pytest
from django.db import connection

from tenants.models import Domain, Tenant


@pytest.mark.django_db(transaction=True)
def test_creating_tenant_provisions_schema() -> None:
    tenant = Tenant(schema_name="acme", name="Acme")
    tenant.save()
    Domain.objects.create(domain="acme.localhost", tenant=tenant, is_primary=True)

    # Primary keys are UUIDv7 (time-ordered, portable across instances).
    assert isinstance(tenant.pk, uuid.UUID)
    assert tenant.pk.version == 7

    # The tenant's schema physically exists in Postgres.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name = %s",
            ["acme"],
        )
        assert cursor.fetchone() is not None
