"""Make an Uruk deployment self-provisioning: migrate, then seed the default tenant.

This is the single startup command (run by ``docker-entrypoint.sh`` before Granian).
It is idempotent — safe to run on every boot — and does two things:

1. ``migrate_schemas`` — apply migrations to the public schema and every tenant
   schema (django-tenants overrides ``migrate`` with this; running it explicitly
   keeps the intent obvious).
2. Provision the **default tenant** eagerly via
   :func:`tenants.default.get_or_create_default_tenant`, so a fresh deployment is
   ready before the first request rather than creating it lazily on that request.
   The same helper backs the fallback middleware, so this shares its concurrency
   safety and demo-page seeding.

The first superuser is *not* created here — that happens on demand via
``POST /api/bootstrap`` (see ``services.auth.claim_superuser``), so no credentials
ever live in the startup path. While the default tenant is unclaimed, the command
prints the claim code that endpoint requires: it lands in the server's logs, where
only the operator can read it.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django_tenants.utils import tenant_context

from services.auth import claim_code
from tenants.default import get_default_tenant, get_or_create_default_tenant


class Command(BaseCommand):
    """Migrate all schemas and idempotently provision the default catch-all tenant."""

    help = "Apply migrations and ensure the default tenant exists (safe to re-run)."

    def handle(self, *args: object, **options: object) -> None:  # noqa: ARG002 - Django's fixed handle() signature
        """Run migrations, then create + seed the default tenant if it is missing."""
        self.stdout.write("Applying migrations (public + tenant schemas)...")
        call_command("migrate_schemas", interactive=False, verbosity=0)

        existed = get_default_tenant() is not None
        tenant = get_or_create_default_tenant()
        verb = "already present" if existed else "created"
        self.stdout.write(self.style.SUCCESS(f"Default tenant {verb}: schema {tenant.schema_name!r} (catch-all for all hosts)."))
        with tenant_context(tenant):
            claimed = User.objects.filter(is_superuser=True).exists()
        if not claimed:
            self.stdout.write(f"Unclaimed: claim the first superuser with POST /api/bootstrap and code {claim_code(tenant.schema_name)}")
