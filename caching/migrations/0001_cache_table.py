"""Create the Postgres cache table in the public schema.

The default cache backend is ``DatabaseCache`` over a single shared table
(``settings.CACHE_TABLE``) living in the public schema — tenant isolation is by
key prefix, not by table (ARCHITECTURE.md §7). Django's cache table is normally
created by the ``createcachetable`` command rather than a migration; we wrap that
same table-creation logic here so a plain ``migrate`` provisions it, and so it is
created in whichever schema this SHARED_APPS migration runs against (public).
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.commands.createcachetable import Command as CreateCacheTable
from django.db import migrations


def create_cache_table(apps, schema_editor) -> None:  # noqa: ANN001, ARG001 - migration signature
    """Create the DatabaseCache backing table if it is not already present."""
    # Only meaningful when the default cache actually is the DB backend; a no-op
    # (harmless) otherwise. createcachetable is idempotent — it checks first.
    command = CreateCacheTable()
    command.verbosity = 0
    command.create_table(database=schema_editor.connection.alias, tablename=settings.CACHE_TABLE, dry_run=False)


def drop_cache_table(apps, schema_editor) -> None:  # noqa: ANN001 - migration signature
    """Drop the cache table on reverse migration."""
    schema_editor.execute(f'DROP TABLE IF EXISTS "{settings.CACHE_TABLE}"')  # noqa: S608 - table name is a trusted setting, not user input


class Migration(migrations.Migration):
    """Provision the shared Postgres cache table."""

    initial = True
    dependencies: list[tuple[str, str]] = []

    operations = [
        migrations.RunPython(create_cache_table, drop_cache_table),
    ]
