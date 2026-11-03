"""Django admin registrations for the public-schema tenant-routing models.

The admin registry is global and ``django.contrib.admin`` is in ``TENANT_APPS``,
so without a guard these models would surface in *every* tenant's admin — and
because the public schema is always on the connection's ``search_path``, a
tenant-schema superuser could edit the shared ``Domain`` table and re-point
another site's hostname at their own tenant. :class:`PublicSchemaOnlyAdmin`
therefore hides and locks these models everywhere except the public schema,
which is only reachable if a deployment explicitly routes a hostname to a
``public`` tenant row — platform-admin territory (ARCHITECTURE.md §4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import admin
from django_tenants.admin import TenantAdminMixin
from django_tenants.utils import get_public_schema_name

from common.tenancy import current_schema
from tenants.models import Domain, Tenant

if TYPE_CHECKING:
    from django.db.models import Model
    from django.http import HttpRequest


def _on_public_schema() -> bool:
    """Return whether the active connection is on the public (shared) schema."""
    return current_schema() == get_public_schema_name()


class PublicSchemaOnlyAdmin(admin.ModelAdmin):
    """A ``ModelAdmin`` that is hidden and fully locked outside the public schema."""

    def has_module_permission(self, request: HttpRequest) -> bool:
        """Hide the app from tenant-schema admin indexes."""
        return _on_public_schema() and super().has_module_permission(request)

    def has_view_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        """Block direct-URL viewing from tenant-schema admins."""
        return _on_public_schema() and super().has_view_permission(request, obj)

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Only the platform admin (public schema) may create rows."""
        return _on_public_schema() and super().has_add_permission(request)

    def has_change_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        """Only the platform admin (public schema) may edit rows."""
        return _on_public_schema() and super().has_change_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        """Only the platform admin (public schema) may delete rows."""
        return _on_public_schema() and super().has_delete_permission(request, obj)


@admin.register(Tenant)
class TenantAdmin(TenantAdminMixin, PublicSchemaOnlyAdmin):
    """Manage tenants.

    Deleting a row does *not* drop its Postgres schema (``auto_drop_schema`` is
    off) — use ``manage.py remove_tenant`` for a full teardown.
    """

    list_display = ("name", "schema_name", "created")
    search_fields = ("name", "schema_name")


@admin.register(Domain)
class DomainAdmin(PublicSchemaOnlyAdmin):
    """Manage the hostname → tenant routing table."""

    list_display = ("domain", "tenant", "is_primary")
    list_filter = ("is_primary",)
    search_fields = ("domain",)
    autocomplete_fields = ("tenant",)
