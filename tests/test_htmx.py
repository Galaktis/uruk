"""htmx dynamic fragments: cached element-subtree rendering + structural invalidation (§9)."""

import pytest
from django.core.cache import cache
from django.test import Client
from django_tenants.utils import tenant_context

from caching.keys import fragment_cache_key
from caching.service import render_fragment_cached
from services import content as svc
from tenants.models import Domain, Tenant


@pytest.mark.django_db(transaction=True)
def test_fragment_caches_and_invalidates_via_ancestor() -> None:
    tenant = Tenant.objects.create(schema_name="frag_inv", name="Frag")

    with tenant_context(tenant):
        cache.clear()
        root = svc.create_element("container", parameters={"tag": "section"})
        child = svc.create_element("text", parent=root, position=0, content="v1")

        assert render_fragment_cached(root) == "<section><p>v1</p></section>"
        assert cache.get(fragment_cache_key(root.pk)) is not None

        # Editing a descendant invalidates the ancestor's cached fragment.
        svc.set_element_content(child, "v2")
        assert cache.get(fragment_cache_key(root.pk)) is None
        assert render_fragment_cached(root) == "<section><p>v2</p></section>"


@pytest.mark.django_db(transaction=True)
def test_fragment_served_over_http() -> None:
    tenant = Tenant.objects.create(schema_name="frag_http", name="FragHttp")
    Domain.objects.create(domain="frag.localhost", tenant=tenant, is_primary=True)
    with tenant_context(tenant):
        element = svc.create_element("raw_html", content="<b>hi</b>")

    response = Client().get(f"/_fragment/{element.pk}", headers={"host": "frag.localhost"})
    assert response.status_code == 200
    assert response.content == b"<b>hi</b>"
    assert response["Content-Type"] == "text/html"
