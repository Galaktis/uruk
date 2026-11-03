"""Reset the deployment to a pristine state: drop every tenant, recreate the default.

Local development regularly needs a truly clean database — e.g. after the committed
seed archive (``tenants/seed/default_site.json``) changes, since seeding runs only once
per tenant and an already-seeded default keeps serving its old rows. This command drops
*every* tenant (schema and all — content, users, tokens, assets), then recreates and
reseeds the default catch-all tenant from the current archive.

Like ``remove_tenant``, it is a trusted-operator escape hatch: destruction is
irreversible, so the ``--yes`` flag is required. Stale cache entries need no special
handling — page/fragment keys embed the dropped rows' UUIDs, so they are unreachable
by the freshly seeded site and simply age out.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django_tenants.utils import get_public_schema_name

from services.tenants import drop_tenant
from tenants.default import get_or_create_default_tenant
from tenants.models import Tenant


class Command(BaseCommand):
    """Drop every tenant and its schema, then recreate the seeded default (requires ``--yes``)."""

    help = "Drop ALL tenants and their schemas, then recreate the seeded default tenant. Irreversible; requires --yes."

    def add_arguments(self, parser: CommandParser) -> None:
        """Register the mandatory confirmation flag."""
        parser.add_argument("--yes", action="store_true", help="Confirm the irreversible deletion of every tenant")

    def handle(self, *args: object, **options: object) -> None:  # noqa: ARG002 - Django's fixed handle() signature
        """Drop every tenant schema, then recreate and reseed the default tenant."""
        if not options["yes"]:
            msg = "Refusing to reset without --yes (this drops EVERY tenant schema and everything in them)"
            raise CommandError(msg)

        # No Tenant row should ever name the public schema, but guard anyway:
        # force-dropping it would take the shared apps' tables down with it.
        schemas = Tenant.objects.exclude(schema_name=get_public_schema_name()).values_list("schema_name", flat=True)
        for schema in list(schemas):
            drop_tenant(schema)
            self.stdout.write(f"Dropped tenant {schema!r} and its schema.")

        tenant = get_or_create_default_tenant()
        self.stdout.write(self.style.SUCCESS(f"Reset complete: default tenant recreated and seeded (schema {tenant.schema_name!r})."))
