"""Public-schema models: tenants and the domains routed to them.

These live in ``SHARED_APPS`` (the public schema). Each :class:`Tenant` owns an
isolated Postgres schema where its content, users, and permissions live.
"""

from django.db import models
from django_tenants.models import DomainMixin, TenantMixin

from common.models import UUID7Model


class Tenant(UUID7Model, TenantMixin):
    """A tenant — one isolated website, backed by its own Postgres schema."""

    name = models.CharField(max_length=256, default="Uruk")
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    # Provision the tenant's schema automatically when the row is created.
    auto_create_schema = True

    def __str__(self) -> str:
        return f"Tenant {self.name} ({self.schema_name})"


class Domain(UUID7Model, DomainMixin):
    """A hostname routed to a tenant."""

    def __str__(self) -> str:
        return str(self.domain)
