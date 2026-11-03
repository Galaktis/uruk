"""Computed-page cache plus dependency-graph invalidation (ARCHITECTURE.md §7).

Rendering is cheap to cache; invalidating correctly is the hard part, because
elements are shared. On each render we persist the set of elements a page's
output depended on; when any of those elements changes, we evict exactly the
pages that used it. That machinery serves the working copy (live mode and the draft
preview); published revisions are immutable and cached without invalidation (§16).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.cache import cache
from django.utils.safestring import SafeString

from caching.keys import fragment_cache_key, page_cache_key, published_fragment_cache_key, published_page_cache_key
from content.models import Element, PageDependency
from rendering import RenderContext, render_element, render_page
from rendering.document import render_document
from services.publishing import snapshot_source

if TYPE_CHECKING:
    from uuid import UUID

    from content.models import Page


def render_page_cached(page: Page) -> SafeString:
    """Return the page's computed HTML from cache, rendering + caching on a miss."""
    key = page_cache_key(page.pk)
    cached = cache.get(key)
    if cached is not None:
        return SafeString(cached)
    ctx = RenderContext()
    html = render_page(page, ctx)
    cache.set(key, str(html))
    _store_dependencies(page, ctx.dependencies)
    return html


def render_fragment_cached(element: Element) -> SafeString:
    """Return an element subtree's cached HTML, rendering + caching on a miss (for htmx)."""
    key = fragment_cache_key(element.pk)
    cached = cache.get(key)
    if cached is not None:
        return SafeString(cached)
    html = render_element(element, RenderContext())
    cache.set(key, str(html))
    return html


def render_published_document(revision_id: UUID, path: str) -> str | None:
    """Return the full HTML document a published revision serves at ``path`` (``None`` → 404).

    Published output is immutable per revision, so it is cached under revision-scoped
    keys with no invalidation at all: publishing just moves serving to new keys (§16).
    """
    key = published_page_cache_key(revision_id, path.strip("/"))
    cached = cache.get(key)
    if cached is not None:
        return cached
    source = snapshot_source(revision_id)
    page = source.resolve_path(path)
    if page is None:
        return None
    html = str(render_document(page, render_page(page, RenderContext(source=source)), source))
    cache.set(key, html)
    return html


def render_published_fragment(revision_id: UUID, element_id: UUID) -> str | None:
    """Return a published element subtree's HTML (``None`` when the revision lacks it)."""
    key = published_fragment_cache_key(revision_id, element_id)
    cached = cache.get(key)
    if cached is not None:
        return cached
    source = snapshot_source(revision_id)
    element = source.element(element_id)
    if element is None:
        return None
    html = str(render_element(element, RenderContext(source=source)))
    cache.set(key, html)
    return html


def _store_dependencies(page: Page, element_ids: set[UUID]) -> None:
    """Rewrite the reverse index of elements this page's output depends on."""
    PageDependency.objects.filter(page=page).delete()
    PageDependency.objects.bulk_create(
        [PageDependency(page=page, element_id=element_id) for element_id in element_ids],
    )


def invalidate_for_element(element: Element) -> None:
    """Evict pages that used this element, and every fragment whose output contains it.

    Page containment is dynamic and comes from the ``PageDependency`` reverse index.
    Fragment containment is structural: an element appears in the fragment of each
    ancestor — and, since ``reference`` elements render another tree in place, in the
    fragments above any reference to it (or to any of its ancestors), transitively.
    """
    page_ids = PageDependency.objects.filter(element_id=element.pk).values_list("page_id", flat=True)
    keys = [page_cache_key(page_id) for page_id in page_ids]
    keys += [fragment_cache_key(fragment_id) for fragment_id in _containing_fragment_ids(element)]
    cache.delete_many(keys)


def _containing_fragment_ids(element: Element) -> set[UUID]:
    """Ids of every element whose rendered subtree contains ``element``.

    Starts from the parent chain, then repeatedly folds in ``reference`` elements
    pointing at anything found so far (plus *their* parent chains), until the set
    stops growing — cycles terminate because visited ids are never re-expanded.
    """
    containing: set[UUID] = set()
    frontier = _ancestor_ids(element)
    while frontier:
        fresh = [fragment_id for fragment_id in frontier if fragment_id not in containing]
        if not fresh:
            break
        containing.update(fresh)
        referrers = Element.objects.filter(kind="reference", parameters__element_id__in=[str(fragment_id) for fragment_id in fresh])
        frontier = [ancestor_id for referrer in referrers for ancestor_id in _ancestor_ids(referrer)]
    return containing


def _ancestor_ids(element: Element) -> list[UUID]:
    """Return the element's own id followed by its ancestors', walking up the tree."""
    ids: list[UUID] = [element.pk]
    parent_id = element.parent_id
    while parent_id is not None:
        ids.append(parent_id)
        parent = Element.objects.filter(pk=parent_id).only("id", "parent_id").first()
        if parent is None:
            break
        parent_id = parent.parent_id
    return ids


def invalidate_for_page(page: Page, *, previous_parent_id: UUID | None = None) -> None:
    """Evict a changed page plus every listing of the collection(s) it belongs to.

    A ``page_list`` records its parent page id as a dependency, so a child page being
    added, edited, moved, or deleted evicts the pages listing that parent — and, for
    htmx, the fragments containing those ``page_list`` elements. A page moved between
    collections refreshes the old one too (``previous_parent_id``, captured pre-save).
    """
    collection_ids = {collection_id for collection_id in (page.pk, page.parent_id, previous_parent_id) if collection_id is not None}
    page_ids = set(PageDependency.objects.filter(element_id__in=collection_ids).values_list("page_id", flat=True))
    keys = [page_cache_key(page_id) for page_id in page_ids | {page.pk}]
    listings = Element.objects.filter(kind="page_list", parameters__parent_id__in=[str(collection_id) for collection_id in collection_ids])
    keys += [fragment_cache_key(fragment_id) for listing in listings for fragment_id in _containing_fragment_ids(listing)]
    cache.delete_many(keys)
