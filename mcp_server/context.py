"""Agent connection context: which tenant and token a tool call acts as.

A connection authenticates with an API token scoped to one tenant (§11). The token
resolves to a Django user inside that tenant's schema, and what the agent can do is
exactly that user's Django permissions — there is no parallel authorization model.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django_tenants.utils import remove_www

from common.tenancy import tenant_connection
from tenants.default import get_or_create_default_tenant
from tenants.models import Domain, Tenant

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass
class AgentContext:
    """The tenant and API token a set of tool calls operates as."""

    tenant: Tenant
    token: str


def build_context_from_env() -> AgentContext:
    """Build the context from ``URUK_MCP_TENANT`` (schema) and ``URUK_MCP_TOKEN``."""
    schema_name = os.environ["URUK_MCP_TENANT"]
    token = os.environ["URUK_MCP_TOKEN"]
    return AgentContext(tenant=Tenant.objects.get(schema_name=schema_name), token=token)


def build_context_from_headers(headers: Mapping[str, str]) -> AgentContext:
    """Build the context of one HTTP request: tenant from ``Host``, token from ``Authorization``.

    The host selects the tenant exactly as the website does (a ``Domain`` match, else the
    default tenant), so an agent connects to ``https://<site>/mcp``. The host is only a
    routing hint, never an identity: the bearer token must still resolve to a user inside
    that tenant's own schema, so pointing another site's token at this host gets nowhere.
    """
    hostname = remove_www(headers.get("host", "").split(":")[0].lower())
    tenant_connection().set_schema_to_public()
    domain = Domain.objects.select_related("tenant").filter(domain=hostname).first()
    tenant = domain.tenant if domain is not None else get_or_create_default_tenant()
    scheme, _, token = headers.get("authorization", "").partition(" ")
    return AgentContext(tenant=tenant, token=token.strip() if scheme.lower() == "bearer" else "")
