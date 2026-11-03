"""Tenant lifecycle operations, gated behind the platform-admin check.

Creating or deleting a tenant is a platform-level action, not a per-site one: only the
default tenant's superuser (the platform operator — see
:func:`services.auth.is_platform_admin`) may do it. The gate is enforced here so every
caller (API, future MCP tool) inherits it.

Creating the :class:`~tenants.models.Tenant` row provisions its Postgres schema
automatically (``auto_create_schema``); a primary :class:`~tenants.models.Domain` routes
its hostname. Deleting a tenant drops its schema — content, users, assets, everything —
which is why the API path refuses to delete the default tenant itself: it is the
catch-all for unmatched hosts and the schema the platform admin's own account lives in.
(Shell operators can still remove any tenant with ``manage.py remove_tenant``.)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django_tenants.utils import get_public_schema_name, schema_context

from services.auth import ensure_platform_admin
from tenants import default as default_tenant
from tenants.models import Domain, Tenant

if TYPE_CHECKING:
    from django.contrib.auth.models import AnonymousUser, User


class DefaultTenantProtectedError(ValueError):
    """Raised when an API caller attempts to delete the default (platform) tenant."""


def create_tenant(user: User | AnonymousUser | None, *, schema: str, name: str, domain: str) -> Tenant:
    """Create a new tenant + primary domain. Requires ``user`` to be a platform admin.

    Tenant/Domain rows live in the public schema, so this switches there to create them —
    the caller is typically mid-request on the default tenant's schema. Raises
    :class:`~services.auth.PlatformPermissionError` if the acting user is not the default
    tenant's superuser.
    """
    ensure_platform_admin(user)
    with schema_context(get_public_schema_name()):
        tenant = Tenant.objects.create(schema_name=schema, name=name)
        Domain.objects.create(domain=domain, tenant=tenant, is_primary=True)
    return tenant


def delete_tenant(user: User | AnonymousUser | None, *, schema: str) -> None:
    """Delete a tenant and drop its Postgres schema. Requires ``user`` to be a platform admin.

    Everything in the schema goes with it — content, users, tokens, assets. The default
    tenant is protected here (:class:`DefaultTenantProtectedError`): it serves every
    unmatched host and holds the platform admin's own account, so removing it over the
    API would saw off the branch the caller sits on. Raises
    :class:`~tenants.models.Tenant.DoesNotExist` for unknown schemas and
    :class:`~services.auth.PlatformPermissionError` for non-admins.
    """
    ensure_platform_admin(user)
    if schema == default_tenant.DEFAULT_TENANT_SCHEMA:
        msg = "The default tenant cannot be deleted over the API"
        raise DefaultTenantProtectedError(msg)
    drop_tenant(schema)


def drop_tenant(schema: str) -> None:
    """Delete a tenant row and drop its schema — ungated, for trusted shell/CI callers.

    The permission gate and default-tenant protection live in :func:`delete_tenant`
    (the API path); this is the raw operation behind it, also exposed as the
    ``manage.py remove_tenant`` command for operators who already hold DB credentials.
    """
    with schema_context(get_public_schema_name()):
        tenant = Tenant.objects.get(schema_name=schema)
        tenant.delete(force_drop=True)
