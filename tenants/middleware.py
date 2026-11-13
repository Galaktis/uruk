"""Tenant routing middleware with a default-tenant fallback.

:class:`django_tenants.middleware.main.TenantMainMiddleware` resolves the request
``Host`` to a :class:`~tenants.models.Domain` and switches the connection to that
tenant's schema. When no ``Domain`` matches it 404s. We override that single hook
so an unmatched hostname falls through to the **default tenant** instead, creating
it on the first ever request (see :mod:`tenants.default`).

Exact ``Domain`` matches are untouched — they still win — so adding real
multi-tenant sites later needs no change here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django_tenants.middleware.main import TenantMainMiddleware

from common.tenancy import tenant_connection
from tenants.default import get_or_create_default_tenant

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


class DefaultTenantFallbackMiddleware(TenantMainMiddleware):
    """Route unmatched hostnames to the (auto-created) default tenant."""

    def no_tenant_found(self, request: HttpRequest, hostname: str) -> HttpResponse | None:
        """Route to the default tenant instead of 404-ing on an unmatched host.

        Mirrors the parent's tenant-selection tail: set ``request.tenant``, switch
        the connection to the tenant schema, and configure URL routing. Returning
        ``None`` lets the request proceed to the resolved view.
        """
        tenant = get_or_create_default_tenant()
        tenant.domain_url = hostname
        request.tenant = tenant  # ty: ignore[missing-attribute] - django-tenants' request attribute
        tenant_connection().set_tenant(tenant)
        self.setup_url_routing(request)
        return None
