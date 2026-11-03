"""Issue an API token for a tenant user (the bootstrap for programmatic/MCP access).

Tokens live in the tenant's schema, so the command runs inside that tenant's context.
The plaintext is printed once and never stored — copy it immediately.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import schema_context

from services import auth as auth_svc

if TYPE_CHECKING:
    from argparse import ArgumentParser


class Command(BaseCommand):
    """Create an API token for ``<username>`` in tenant schema ``<schema>``."""

    help = "Issue an API token for a tenant user; prints the plaintext token once."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Declare the command's positional and optional arguments."""
        parser.add_argument("schema", help="Tenant schema name the user lives in.")
        parser.add_argument("username", help="Username to issue the token for.")
        parser.add_argument("--name", default="", help="Human label for the token.")

    def handle(self, *args: object, **options: object) -> None:  # noqa: ARG002 - Django's fixed handle() signature
        """Resolve the user in the tenant schema and print a freshly-issued token."""
        schema = str(options["schema"])
        username = str(options["username"])
        with schema_context(schema):
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist as exc:
                msg = f"No user {username!r} in schema {schema!r}"
                raise CommandError(msg) from exc
            _, raw = auth_svc.create_token(user, name=str(options["name"]))
        self.stdout.write(self.style.SUCCESS(raw))
