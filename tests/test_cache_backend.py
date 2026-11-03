"""The default cache backend is Postgres, shared across tenants by key-prefix (§7).

With no ``URUK_REDIS_URL`` in the environment the cache is Django's ``DatabaseCache``
over one ``uruk_cache`` table in the public schema. Isolation is by the tenant key
prefix, not by table — these tests pin both properties.
"""

from uuid import UUID

import pytest
from django.core.cache import cache, caches
from django.core.cache.backends.db import DatabaseCache
from django_tenants.utils import tenant_context

from caching.keys import page_cache_key
from tenants.models import Tenant


def test_default_backend_is_database_cache() -> None:
    """Without Redis configured, the default cache is Postgres DatabaseCache."""
    # `caches["default"]` is the concrete backend; the module-level `cache` is a proxy.
    assert isinstance(caches["default"], DatabaseCache)


@pytest.mark.django_db(transaction=True)
def test_same_key_isolated_across_tenants_in_shared_table() -> None:
    """The one shared cache table keeps tenants apart via the schema key-prefix."""
    tenant_a = Tenant.objects.create(schema_name="cache_iso_a", name="A")
    tenant_b = Tenant.objects.create(schema_name="cache_iso_b", name="B")
    key = page_cache_key(UUID("00000000-0000-0000-0000-000000000001"))

    with tenant_context(tenant_a):
        cache.set(key, "<p>A</p>")
    with tenant_context(tenant_b):
        # Same logical key, different tenant → must not see A's value.
        assert cache.get(key) is None
        cache.set(key, "<p>B</p>")
    with tenant_context(tenant_a):
        assert cache.get(key) == "<p>A</p>"
