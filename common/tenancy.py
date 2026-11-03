"""Typed access to django-tenants' schema-aware database connection.

``django.db.connection`` is typed as Django's generic ``BaseDatabaseWrapper``, which
knows nothing of tenants. The configured ENGINE is ``django_tenants.postgresql_backend``,
so the connection *is* django-tenants' wrapper; these helpers say so once, for the type
checker, instead of at every call site.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from django.db import connection

if TYPE_CHECKING:
    from django_tenants.postgresql_backend.base import DatabaseWrapper


def tenant_connection() -> DatabaseWrapper:
    """Return the default connection as django-tenants' schema-aware wrapper."""
    return cast("DatabaseWrapper", connection)


def current_schema() -> str:
    """Return the schema the connection is currently set to (``public`` outside a tenant)."""
    return str(tenant_connection().schema_name)
