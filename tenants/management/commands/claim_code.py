"""Print the claim code a tenant's first superuser needs (``POST /api/bootstrap``)."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError, CommandParser

from services.auth import claim_code
from tenants.default import DEFAULT_TENANT_SCHEMA
from tenants.models import Tenant


class Command(BaseCommand):
    """Print the claim code for a tenant (the default tenant when no schema is given)."""

    help = "Print the claim code for a tenant's first-superuser claim."

    def add_arguments(self, parser: CommandParser) -> None:
        """Accept an optional tenant schema name."""
        parser.add_argument("schema_name", nargs="?", default=DEFAULT_TENANT_SCHEMA)

    def handle(self, *args: object, **options: object) -> None:  # noqa: ARG002 - Django's fixed handle() signature
        """Print the code, refusing unknown schemas."""
        schema = str(options["schema_name"])
        if not Tenant.objects.filter(schema_name=schema).exists():
            msg = f"No tenant with schema {schema!r}"
            raise CommandError(msg)
        self.stdout.write(claim_code(schema))
