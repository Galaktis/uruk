"""Provision the default (catch-all) tenant explicitly.

The default tenant is normally created lazily on the first unmatched request
(:mod:`tenants.middleware`). This command does the same eagerly — handy to
pre-warm a fresh deployment or to seed the default website from a shell/CI step.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from tenants.default import DEFAULT_TENANT_SCHEMA, get_default_tenant, get_or_create_default_tenant


class Command(BaseCommand):
    """Create the default tenant and seed its default website if it does not exist."""

    help = "Ensure the default catch-all tenant exists (create + seed the default website if missing)."

    def handle(self, *args: object, **options: object) -> None:  # noqa: ARG002 - Django's fixed handle() signature
        """Create the default tenant if absent; report what happened."""
        existed = get_default_tenant() is not None
        tenant = get_or_create_default_tenant()
        verb = "already present" if existed else "created"
        self.stdout.write(self.style.SUCCESS(f"Default tenant {verb}: schema '{tenant.schema_name}' ({DEFAULT_TENANT_SCHEMA})."))
