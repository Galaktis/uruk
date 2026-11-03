"""Computed-page caching + dependency-aware invalidation across shared elements (§7)."""

import pytest
from django.core.cache import cache
from django_tenants.utils import tenant_context

from caching.keys import fragment_cache_key, page_cache_key
from caching.service import render_fragment_cached, render_page_cached
from content.models import Element, Page, PageDependency
from tenants.models import Tenant


@pytest.mark.django_db(transaction=True)
def test_shared_element_change_invalidates_all_dependent_pages() -> None:
    tenant = Tenant.objects.create(schema_name="cache_t", name="Cache")

    with tenant_context(tenant):
        cache.clear()
        # One element tree used as the root of two different pages.
        root = Element.objects.create(kind="container")
        child = Element.objects.create(kind="raw_html", parent=root, content="<p>v1</p>")
        page_a = Page.objects.create(slug="a", name="A", root_element=root)
        page_b = Page.objects.create(slug="b", name="B", root_element=root)

        html_a = render_page_cached(page_a)
        html_b = render_page_cached(page_b)
        assert "v1" in html_a
        assert "v1" in html_b

        # Both pages are now cached, and both recorded a dependency on the shared child.
        assert cache.get(page_cache_key(page_a.pk)) is not None
        assert cache.get(page_cache_key(page_b.pk)) is not None
        assert PageDependency.objects.filter(element_id=child.pk).count() == 2

        # Editing the shared element must evict every page that used it.
        child.content = "<p>v2</p>"
        child.save()
        assert cache.get(page_cache_key(page_a.pk)) is None
        assert cache.get(page_cache_key(page_b.pk)) is None

        # Re-rendering now reflects the change for both pages.
        assert "v2" in render_page_cached(page_a)
        assert "v2" in render_page_cached(page_b)


@pytest.mark.django_db(transaction=True)
def test_fragment_above_a_reference_evicts_when_the_target_changes() -> None:
    tenant = Tenant.objects.create(schema_name="cache_ref", name="CacheRef")

    with tenant_context(tenant):
        cache.clear()
        shared = Element.objects.create(kind="raw_html", content="<p>v1</p>", is_reusable=True)
        fragment_root = Element.objects.create(kind="container")
        Element.objects.create(kind="reference", parent=fragment_root, parameters={"element_id": str(shared.pk)})

        assert "v1" in render_fragment_cached(fragment_root)
        assert cache.get(fragment_cache_key(fragment_root.pk)) is not None

        # Editing the referenced element evicts fragments above the reference too,
        # even though the target lives in a different tree than the fragment root.
        shared.content = "<p>v2</p>"
        shared.save()
        assert cache.get(fragment_cache_key(fragment_root.pk)) is None
        assert "v2" in render_fragment_cached(fragment_root)
