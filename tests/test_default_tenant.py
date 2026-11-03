"""Default-tenant fallback: an unmatched Host self-bootstraps one catch-all tenant.

A fresh deployment has no tenants; the first request to any hostname should create a
single default tenant — seeded with the packaged default website, a full styled site
restored from ``tenants/seed/default_site.json`` — and serve it. Specific Domain rows
must still win over the fallback, and the default tenant must be created only once no
matter how many hosts hit it.
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django_tenants.utils import tenant_context

from services import content as svc
from services.publishing import publish
from tenants.default import DEFAULT_TENANT_SCHEMA, get_default_tenant
from tenants.models import Domain, Tenant

#: A phrase that only the seeded default site contains (home page title + footer).
SEED_MARKER = "a website is data, not code"


@pytest.mark.django_db(transaction=True)
def test_unmatched_host_creates_and_serves_default_tenant() -> None:
    assert get_default_tenant() is None  # nothing exists yet

    response = Client().get("/", headers={"host": "anything.example.com"})

    assert response.status_code == 200
    body = response.content.decode()
    assert body.startswith("<!DOCTYPE html>")
    assert SEED_MARKER in body

    tenant = get_default_tenant()
    assert tenant is not None
    assert tenant.schema_name == DEFAULT_TENANT_SCHEMA
    with tenant_context(tenant):
        assert svc.Page.objects.filter(slug=svc.HOME_SLUG).exists()


@pytest.mark.django_db(transaction=True)
def test_default_tenant_created_only_once_across_hosts() -> None:
    Client().get("/", headers={"host": "a.example.com"})
    Client().get("/", headers={"host": "b.example.com"})
    Client().get("/", headers={"host": "c.example.com"})

    assert Tenant.objects.filter(schema_name=DEFAULT_TENANT_SCHEMA).count() == 1


@pytest.mark.django_db(transaction=True)
def test_specific_domain_wins_over_default() -> None:
    # A real site on an explicit domain.
    real = Tenant.objects.create(schema_name="real_site", name="Real")
    Domain.objects.create(domain="real.localhost", tenant=real, is_primary=True)
    with tenant_context(real):
        root = svc.create_element("text", content="the real site")
        svc.create_page(slug=svc.HOME_SLUG, name="Real Home", root_element=root)

    matched = Client().get("/", headers={"host": "real.localhost"})
    assert matched.status_code == 200
    assert "the real site" in matched.content.decode()
    assert SEED_MARKER not in matched.content.decode()

    # An unmatched host still falls through to the default tenant, not the real one.
    fell_through = Client().get("/", headers={"host": "stranger.example.com"})
    assert fell_through.status_code == 200
    assert SEED_MARKER in fell_through.content.decode()


@pytest.mark.django_db(transaction=True)
def test_default_tenant_is_reused_and_not_reseeded() -> None:
    Client().get("/", headers={"host": "first.example.com"})
    tenant = get_default_tenant()
    assert tenant is not None

    # Edit the seeded home page, then hit an unmatched host again.
    with tenant_context(tenant):
        page_count = svc.Page.objects.count()
        page = svc.get_page(svc.HOME_SLUG)
        svc.update_page(page, name="Edited By User")

    Client().get("/", headers={"host": "second.example.com"})

    with tenant_context(tenant):
        # Same pages as before, and our edit survived (no re-seed clobbered it).
        assert svc.Page.objects.count() == page_count
        assert svc.get_page(svc.HOME_SLUG).name == "Edited By User"


# The seeded website itself ----------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_seeded_site_serves_every_page_with_shared_chrome() -> None:
    client = Client()
    home = client.get("/", headers={"host": "seed.example.com"}).content.decode()

    for path in ("/", "/manifesto", "/architecture", "/quickstart", "/configuration"):
        response = client.get(path, headers={"host": "seed.example.com"})
        assert response.status_code == 200, path
        body = response.content.decode()
        # The shared header/footer elements are included by reference on every page.
        assert 'class="site-head"' in body, path
        assert 'class="site-foot"' in body, path

    # The head is data: the site.head_html setting links the stylesheet asset.
    match = re.search(r'href="(/_assets/[0-9a-f]{64}/site\.css)"', home)
    assert match is not None

    css = client.get(match.group(1), headers={"host": "seed.example.com"})
    assert css.status_code == 200
    assert css["Content-Type"] == "text/css"
    assert "immutable" in css["Cache-Control"]
    assert "Martian Mono" in css.content.decode()


@pytest.mark.django_db(transaction=True)
def test_editing_the_shared_header_invalidates_every_page() -> None:
    client = Client()
    host = {"host": "chrome.example.com"}
    client.get("/", headers=host)  # bootstrap the (published) default site

    tenant = get_default_tenant()
    assert tenant is not None
    with tenant_context(tenant):
        client.force_login(User.objects.create_superuser("editor", "editor@example.com", "pw"))
    client.get("/_preview/", headers=host)  # warm the working-copy cache
    client.get("/_preview/manifesto", headers=host)

    with tenant_context(tenant):
        # The tagline is a *nested child* of the shared header tree — dependency
        # tracking records it through the reference, so one child edit is enough.
        tagline = svc.Element.objects.get(content__contains='class="head-tag"')
        svc.set_element_content(tagline, tagline.content + "<!-- header-v2 -->")

    # One element edit; every page that references the header re-renders fresh...
    assert "header-v2" in client.get("/_preview/", headers=host).content.decode()
    assert "header-v2" in client.get("/_preview/manifesto", headers=host).content.decode()
    # ...in the draft only: the seeded site is published, so visitors see no change yet.
    assert "header-v2" not in client.get("/", headers=host).content.decode()

    with tenant_context(tenant):
        publish(message="New tagline")
    assert "header-v2" in client.get("/", headers=host).content.decode()
    assert "header-v2" in client.get("/manifesto", headers=host).content.decode()


@pytest.mark.django_db(transaction=True)
def test_blank_default_site_serves_an_empty_published_landing_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tenants.default.DEFAULT_SITE", "blank")
    # Earlier tests leave the default schema (and its showcase pages) behind: start clean.
    with connection.cursor() as cursor:
        cursor.execute(f'DROP SCHEMA IF EXISTS "{DEFAULT_TENANT_SCHEMA}" CASCADE')

    response = Client().get("/", headers={"host": "example.org"})

    assert response.status_code == 200
    body = response.content.decode()
    assert "<main></main>" in body
    assert SEED_MARKER not in body
    tenant = get_default_tenant()
    assert tenant is not None
    with tenant_context(tenant):
        assert [page.slug for page in svc.list_pages()] == ["index"]
