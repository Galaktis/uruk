"""The default (catch-all) tenant.

Uruk is multi-tenant, but a fresh deployment has no tenants at all — the first
request would 404 with nowhere to route. To make the system self-bootstrapping,
a single **default tenant** answers any hostname that doesn't match a specific
:class:`~tenants.models.Domain` row. It is created once, on demand, and seeded
with the packaged default website (``tenants/seed/default_site.json``, a portability
archive built by ``scripts/build_default_site.py``) so the very first request serves
a complete, styled site — itself made of nothing but rows.

This is deliberately *one* tenant, not one-per-hostname: there is no unbounded
schema creation, hence no attack surface from stray ``Host`` headers. Real sites
are added later as explicit ``Domain`` rows, which take precedence (see
:mod:`tenants.middleware`).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from django.db import transaction
from django_tenants.utils import schema_exists, tenant_context

from content.models import Page
from services import content as svc
from services.portability import restore_content
from services.publishing import publish
from tenants.models import Domain, Tenant

#: Schema name of the default tenant. Overridable so a deployment can pin its own.
DEFAULT_TENANT_SCHEMA = os.environ.get("URUK_DEFAULT_TENANT_SCHEMA", "default")

#: Catch-all domain recorded for the default tenant. ``django-tenants`` requires a
#: primary ``Domain`` per tenant; this hostname is a placeholder — routing to the
#: default tenant happens by *fallback* (middleware), not by matching this string.
DEFAULT_TENANT_DOMAIN = os.environ.get("URUK_DEFAULT_TENANT_DOMAIN", "default.localhost")


def get_default_tenant() -> Tenant | None:
    """Return the default tenant if it exists, else ``None`` (no side effects)."""
    return Tenant.objects.filter(schema_name=DEFAULT_TENANT_SCHEMA).first()


def get_or_create_default_tenant() -> Tenant:
    """Return the default tenant, creating and seeding it on first call.

    Idempotent and concurrency-safe: the create is wrapped in a transaction and
    guards against a racing request having created the schema already. Subsequent
    calls are a single indexed lookup.
    """
    tenant = get_default_tenant()
    if tenant is not None:
        return tenant

    with transaction.atomic():
        # Re-check inside the transaction; a concurrent request may have won.
        tenant = Tenant.objects.select_for_update().filter(schema_name=DEFAULT_TENANT_SCHEMA).first()
        if tenant is not None:
            return tenant

        # ``auto_create_schema`` on the model provisions and migrates the schema on
        # save. If a schema with this name somehow already exists (e.g. left over),
        # skip auto-creation to avoid a migration error and adopt it.
        tenant = Tenant(schema_name=DEFAULT_TENANT_SCHEMA, name="Uruk")
        if schema_exists(DEFAULT_TENANT_SCHEMA):
            tenant.auto_create_schema = False
        tenant.save()
        Domain.objects.get_or_create(
            domain=DEFAULT_TENANT_DOMAIN,
            defaults={"tenant": tenant, "is_primary": True},
        )

    _seed_default_site(tenant)
    return tenant


#: The website a fresh default tenant is seeded with — a portability archive
#: committed with the repository and rebuilt via ``scripts/build_default_site.py``.
SEED_ARCHIVE_PATH = Path(__file__).resolve().parent / "seed" / "default_site.json"

#: Which site a fresh default tenant starts with: ``showcase`` (the packaged Uruk
#: website) or ``blank`` (a single empty landing page, for a real deployment that
#: will author its own site through MCP or the Studio).
DEFAULT_SITE = os.environ.get("URUK_DEFAULT_SITE", "showcase")


def _seed_default_site(tenant: Tenant) -> None:
    """Seed the default tenant's schema with its starting website (once).

    The seed is published straight away, so a fresh site starts in draft mode: edits
    stay invisible to visitors until the next publish (ARCHITECTURE.md §16).
    """
    with tenant_context(tenant):
        if Page.objects.exists():
            return  # already seeded (or user-authored) — leave it alone
        if DEFAULT_SITE == "blank":
            _seed_blank_site()
            publish(message="Blank site")
            return
        archive = json.loads(SEED_ARCHIVE_PATH.read_text(encoding="utf-8"))
        restore_content(archive)
        publish(message="Default site")


def _seed_blank_site() -> None:
    """Create the minimal blank site: one home page holding an empty ``<main>``."""
    root = svc.create_element("container", parameters={"tag": "main"})
    svc.create_page("index", "Home", root_element=root)
