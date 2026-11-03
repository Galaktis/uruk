"""Public page routing: slug-path resolution + full-document rendering over HTTP."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from django.core.cache import cache
from django.test import Client
from django_tenants.utils import tenant_context

from caching.keys import page_cache_key
from services import content as svc
from tenants.models import Domain, Tenant

if TYPE_CHECKING:
    from content.models import Page


def _page_with_text(slug: str, name: str, text: str, *, parent: Page | None = None) -> Page:
    """Create a page whose root element renders a single paragraph of ``text``."""
    root = svc.create_element("text", content=text)
    return svc.create_page(slug=slug, name=name, parent=parent, root_element=root)


@pytest.mark.django_db(transaction=True)
def test_home_page_served_as_full_document() -> None:
    tenant = Tenant.objects.create(schema_name="route_home", name="Home")
    Domain.objects.create(domain="home.localhost", tenant=tenant, is_primary=True)
    with tenant_context(tenant):
        _page_with_text(svc.HOME_SLUG, "Welcome", "hello world")

    response = Client().get("/", headers={"host": "home.localhost"})

    assert response.status_code == 200
    assert response["Content-Type"] == "text/html"
    body = response.content.decode()
    assert body.startswith("<!DOCTYPE html>")
    assert '<html lang="en">' in body
    assert "<title>Welcome</title>" in body
    assert "<p>hello world</p>" in body


@pytest.mark.django_db(transaction=True)
def test_document_language_comes_from_the_page_then_the_site() -> None:
    tenant = Tenant.objects.create(schema_name="route_lang", name="Lang")
    Domain.objects.create(domain="lang.localhost", tenant=tenant, is_primary=True)
    with tenant_context(tenant):
        _page_with_text(svc.HOME_SLUG, "Accueil", "bonjour")
        svc.update_page(_page_with_text("about", "About", "hello"), parameters={"lang": "en-GB"})
        svc.set_setting("site.lang", "fr")

    client = Client()
    assert '<html lang="fr">' in client.get("/", headers={"host": "lang.localhost"}).content.decode()
    assert '<html lang="en-GB">' in client.get("/about/", headers={"host": "lang.localhost"}).content.decode()


@pytest.mark.django_db(transaction=True)
def test_nested_slug_path_resolves_down_the_tree() -> None:
    tenant = Tenant.objects.create(schema_name="route_nested", name="Nested")
    Domain.objects.create(domain="nested.localhost", tenant=tenant, is_primary=True)
    with tenant_context(tenant):
        blog = _page_with_text("blog", "Blog", "blog index")
        _page_with_text("my-post", "My Post", "the post body", parent=blog)

    response = Client().get("/blog/my-post/", headers={"host": "nested.localhost"})

    assert response.status_code == 200
    assert "<title>My Post</title>" in response.content.decode()
    assert "<p>the post body</p>" in response.content.decode()


@pytest.mark.django_db(transaction=True)
def test_unknown_path_returns_404() -> None:
    tenant = Tenant.objects.create(schema_name="route_404", name="NotFound")
    Domain.objects.create(domain="notfound.localhost", tenant=tenant, is_primary=True)
    with tenant_context(tenant):
        _page_with_text(svc.HOME_SLUG, "Home", "home")

    response = Client().get("/does-not-exist/", headers={"host": "notfound.localhost"})
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_page_with_no_root_element_renders_empty_body() -> None:
    tenant = Tenant.objects.create(schema_name="route_empty", name="Empty")
    Domain.objects.create(domain="empty.localhost", tenant=tenant, is_primary=True)
    with tenant_context(tenant):
        svc.create_page(slug=svc.HOME_SLUG, name="Blank")

    response = Client().get("/", headers={"host": "empty.localhost"})
    assert response.status_code == 200
    assert "<body></body>" in response.content.decode()


@pytest.mark.django_db(transaction=True)
def test_reserved_prefixes_are_not_shadowed_by_catch_all() -> None:
    tenant = Tenant.objects.create(schema_name="route_reserved", name="Reserved")
    Domain.objects.create(domain="reserved.localhost", tenant=tenant, is_primary=True)
    with tenant_context(tenant):
        element = svc.create_element("raw_html", content="<b>frag</b>")

    # The fragment endpoint keeps working — the catch-all must be registered last.
    response = Client().get(f"/_fragment/{element.pk}", headers={"host": "reserved.localhost"})
    assert response.status_code == 200
    assert response.content == b"<b>frag</b>"


@pytest.mark.django_db(transaction=True)
def test_served_page_is_cached() -> None:
    tenant = Tenant.objects.create(schema_name="route_cache", name="Cache")
    Domain.objects.create(domain="cache.localhost", tenant=tenant, is_primary=True)
    with tenant_context(tenant):
        cache.clear()
        page = _page_with_text(svc.HOME_SLUG, "Cached", "cached body")

    response = Client().get("/", headers={"host": "cache.localhost"})
    assert response.status_code == 200

    with tenant_context(tenant):
        assert cache.get(page_cache_key(page.pk)) == "<p>cached body</p>"


@pytest.mark.django_db(transaction=True)
def test_routing_is_tenant_isolated() -> None:
    tenant_a = Tenant.objects.create(schema_name="route_iso_a", name="A")
    Domain.objects.create(domain="a.localhost", tenant=tenant_a, is_primary=True)
    tenant_b = Tenant.objects.create(schema_name="route_iso_b", name="B")
    Domain.objects.create(domain="b.localhost", tenant=tenant_b, is_primary=True)

    with tenant_context(tenant_a):
        _page_with_text("only-on-a", "Only A", "a body")

    # Tenant A serves its page...
    assert Client().get("/only-on-a/", headers={"host": "a.localhost"}).status_code == 200
    # ...but tenant B has no such page and must 404, not leak A's content.
    assert Client().get("/only-on-a/", headers={"host": "b.localhost"}).status_code == 404
