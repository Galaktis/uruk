"""The content service layer creates/edits content and integrates with caching."""

import pytest
from django.core.cache import cache
from django_tenants.utils import tenant_context

from caching.keys import page_cache_key
from caching.service import render_page_cached
from content.models import Element
from services import content as svc
from tenants.models import Tenant


@pytest.mark.django_db(transaction=True)
def test_build_page_via_service_and_render() -> None:
    tenant = Tenant.objects.create(schema_name="svc_build", name="Build")

    with tenant_context(tenant):
        root = svc.create_element("container", parameters={"tag": "section"})
        svc.create_element("heading", parent=root, position=0, parameters={"level": 1}, content="Title")
        svc.create_element("text", parent=root, position=1, content="Body")
        page = svc.create_page("index", "Index", root_element=root)

        html = render_page_cached(page)
        assert html == "<section><h1>Title</h1><p>Body</p></section>"
        assert svc.get_page("index").pk == page.pk
        assert [p.pk for p in svc.list_pages()] == [page.pk]


@pytest.mark.django_db(transaction=True)
def test_service_edit_invalidates_cached_page() -> None:
    tenant = Tenant.objects.create(schema_name="svc_edit", name="Edit")

    with tenant_context(tenant):
        cache.clear()
        root = svc.create_element("container")
        text = svc.create_element("text", parent=root, position=0, content="before")
        page = svc.create_page("index", "Index", root_element=root)

        assert "before" in render_page_cached(page)
        assert cache.get(page_cache_key(page.pk)) is not None

        # Editing through the service must evict the cached page (via the save signal).
        svc.set_element_content(text, "after")
        assert cache.get(page_cache_key(page.pk)) is None
        assert "after" in render_page_cached(page)


@pytest.mark.django_db(transaction=True)
def test_settings_and_assets() -> None:
    tenant = Tenant.objects.create(schema_name="svc_misc", name="Misc")

    with tenant_context(tenant):
        svc.set_setting("site.title", {"text": "Uruk"})
        svc.set_setting("site.title", {"text": "Uruk CMS"})  # upsert
        assert svc.get_setting("site.title") == {"text": "Uruk CMS"}
        assert svc.get_setting("missing", default="fallback") == "fallback"
        assert svc.get_settings() == {"site.title": {"text": "Uruk CMS"}}

        asset = svc.upload_asset("main.css", "text/css", b"body{}")
        assert len(asset.sha256) == 64


@pytest.mark.django_db(transaction=True)
def test_reorder_children() -> None:
    tenant = Tenant.objects.create(schema_name="svc_reorder", name="Reorder")

    with tenant_context(tenant):
        root = svc.create_element("container")
        a = svc.create_element("text", parent=root, position=0, content="a")
        b = svc.create_element("text", parent=root, position=1, content="b")

        svc.reorder_children(root, [b.pk, a.pk])
        ordered = [c.content for c in Element.objects.filter(parent=root)]
        assert ordered == ["b", "a"]
