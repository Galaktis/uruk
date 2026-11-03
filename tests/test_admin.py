"""Django admin: per-tenant content administration, public-schema-only routing models.

The admin is a per-tenant debugging surface: a tenant superuser can browse the CMS
models of *their* schema, but the shared ``tenants`` app (Tenant/Domain rows in the
public schema, writable from every tenant connection) must stay hidden and locked —
otherwise a tenant superuser could re-point another site's domain at their tenant.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client, RequestFactory
from django_tenants.utils import tenant_context

from tenants.admin import DomainAdmin, TenantAdmin
from tenants.models import Domain, Tenant

CONTENT_CHANGELISTS = [
    "/uruk/content/element/",
    "/uruk/content/page/",
    "/uruk/content/asset/",
    "/uruk/content/setting/",
    "/uruk/content/pagedependency/",
    "/uruk/content/apitoken/",
]


@pytest.fixture(autouse=True)
def _plain_staticfiles(settings) -> None:  # noqa: ANN001 - pytest-django fixture
    """Serve admin static files without the collectstatic manifest.

    Tests run with ``DEBUG=False``, where the production manifest storage raises on
    any ``{% static %}`` not present in a collected manifest; swap in the plain
    storage so admin pages render without a ``collectstatic`` step.
    """
    settings.STORAGES = {**settings.STORAGES, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}


def _tenant_with_admin(schema: str, host: str) -> Tenant:
    tenant = Tenant.objects.create(schema_name=schema, name=schema)
    Domain.objects.create(domain=host, tenant=tenant, is_primary=True)
    with tenant_context(tenant):
        User.objects.create_superuser("admin", "admin@example.com", "pw")
    return tenant


@pytest.mark.django_db(transaction=True)
def test_tenant_superuser_browses_content_models(client) -> None:  # noqa: ANN001 - pytest-django fixture
    _tenant_with_admin("admin_content", "admin-content.localhost")
    headers = {"host": "admin-content.localhost"}

    login = client.post("/uruk/login/", {"username": "admin", "password": "pw", "next": "/uruk/"}, headers=headers)
    assert login.status_code == 302

    index = client.get("/uruk/", headers=headers)
    assert index.status_code == 200
    for url in CONTENT_CHANGELISTS:
        assert client.get(url, headers=headers).status_code == 200, url


@pytest.mark.django_db(transaction=True)
def test_tenant_admin_hides_and_locks_the_shared_routing_models(client) -> None:  # noqa: ANN001 - pytest-django fixture
    _tenant_with_admin("admin_iso", "admin-iso.localhost")
    headers = {"host": "admin-iso.localhost"}
    assert client.post("/uruk/login/", {"username": "admin", "password": "pw", "next": "/uruk/"}, headers=headers).status_code == 302

    # The tenants app is absent from the index...
    index = client.get("/uruk/", headers=headers)
    assert b"/uruk/tenants/" not in index.content
    # ...and direct URLs are denied outright, superuser or not: the shared routing
    # tables are platform-admin territory.
    assert client.get("/uruk/tenants/tenant/", headers=headers).status_code == 403
    assert client.get("/uruk/tenants/domain/", headers=headers).status_code == 403


@pytest.mark.django_db(transaction=True)
def test_routing_models_are_administrable_from_the_public_schema() -> None:
    """On the public schema (platform-admin territory) the guard opens up."""
    from django.contrib import admin  # noqa: PLC0415 - test-local import

    request = RequestFactory().get("/uruk/")
    request.user = User.objects.create_superuser("platform", "p@example.com", "pw")  # public schema: the connection default in tests

    for admin_cls, model in ((TenantAdmin, Tenant), (DomainAdmin, Domain)):
        model_admin = admin_cls(model, admin.site)
        assert model_admin.has_module_permission(request)
        assert model_admin.has_view_permission(request)
        assert model_admin.has_change_permission(request)


@pytest.mark.django_db(transaction=True)
def test_admin_lives_under_uruk_admin_path_not_the_well_known_admin() -> None:
    """The admin is mounted at /<URUK_ADMIN_PATH>/ (default ``uruk``); /admin/ is just an unknown page."""
    tenant = Tenant.objects.create(schema_name="admin_path", name="Path")
    Domain.objects.create(domain="admin-path.localhost", tenant=tenant, is_primary=True)
    headers = {"host": "admin-path.localhost"}

    assert Client().get("/uruk/login/", headers=headers).status_code == 200
    assert Client().get("/admin/login/", headers=headers).status_code == 404
