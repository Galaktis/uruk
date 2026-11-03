"""Tenant data stays isolated across schema switches on pooled connections (§3 risk #2)."""

import pytest
from django.contrib.auth.models import User
from django_tenants.utils import tenant_context

from tenants.models import Tenant


@pytest.mark.django_db(transaction=True)
def test_tenant_data_is_isolated_under_pool() -> None:
    t1 = Tenant.objects.create(schema_name="t1", name="T1")
    t2 = Tenant.objects.create(schema_name="t2", name="T2")

    with tenant_context(t1):
        User.objects.create_user(username="alice")
    with tenant_context(t2):
        User.objects.create_user(username="bob")

    # Switching back to each tenant reuses pooled connections; the search_path must
    # be reset per switch so neither tenant ever sees the other's rows.
    with tenant_context(t1):
        assert set(User.objects.values_list("username", flat=True)) == {"alice"}
    with tenant_context(t2):
        assert set(User.objects.values_list("username", flat=True)) == {"bob"}
