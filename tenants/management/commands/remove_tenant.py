"""Delete a tenant and drop its Postgres schema from the shell.

The API path (``DELETE /api/tenants/{schema}``) is gated on the platform admin and
refuses the default tenant. This command is the trusted-operator escape hatch: whoever
can run ``manage.py`` already holds the database credentials, so it can remove *any*
tenant — including a stale default tenant, which the fallback middleware will simply
recreate and reseed on the next unmatched request. Destruction is irreversible, so the
``--yes`` flag is required.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError, CommandParser

from services.tenants import drop_tenant
from tenants.models import Tenant


class Command(BaseCommand):
    """Delete a tenant row and drop its schema (requires ``--yes``)."""

    help = "Delete a tenant and drop its Postgres schema. Irreversible; requires --yes."

    def add_arguments(self, parser: CommandParser) -> None:
        """Register the schema name and the mandatory confirmation flag."""
        parser.add_argument("schema_name", help="Schema name of the tenant to delete")
        parser.add_argument("--yes", action="store_true", help="Confirm the irreversible deletion")

    def handle(self, *args: object, **options: object) -> None:  # noqa: ARG002 - Django's fixed handle() signature
        """Validate the confirmation flag, then delete the tenant and its schema."""
        schema = str(options["schema_name"])
        if not options["yes"]:
            msg = f"Refusing to delete tenant {schema!r} without --yes (this drops the schema and everything in it)"
            raise CommandError(msg)
        try:
            drop_tenant(schema)
        except Tenant.DoesNotExist as exc:
            msg = f"No tenant with schema {schema!r}"
            raise CommandError(msg) from exc
        self.stdout.write(self.style.SUCCESS(f"Deleted tenant {schema!r} and dropped its schema."))
