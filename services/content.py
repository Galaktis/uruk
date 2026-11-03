"""Content operations: pages, elements, settings, and assets.

Callers run inside a tenant context; these functions use the ORM directly and
rely on the cache-invalidation signals (ARCHITECTURE.md §7) firing on save/delete.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from content.models import Asset, Element, Page, Setting
from services.storage import get_storage_backend

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID


# Pages -----------------------------------------------------------------------
def create_page(
    slug: str,
    name: str,
    *,
    parent: Page | None = None,
    root_element: Element | None = None,
    parameters: dict | None = None,
    position: int = 0,
) -> Page:
    """Create a page."""
    return Page.objects.create(
        slug=slug,
        name=name,
        parent=parent,
        root_element=root_element,
        parameters=parameters or {},
        position=position,
    )


def update_page(page: Page, *, slug: str | None = None, name: str | None = None, parameters: dict | None = None) -> Page:
    """Update a page's slug, name, and/or parameters (``None`` leaves a field unchanged)."""
    if slug is not None:
        page.slug = slug
    if name is not None:
        page.name = name
    if parameters is not None:
        page.parameters = parameters
    page.save()
    return page


def set_page_root_element(page: Page, root_element: Element | None) -> Page:
    """Set (or clear) the page's root element."""
    page.root_element = root_element
    page.save()
    return page


def delete_page(page: Page) -> None:
    """Delete a page."""
    page.delete()


def get_page(slug: str) -> Page:
    """Fetch a page by slug."""
    return Page.objects.get(slug=slug)


#: Slug of the top-level page served for the site root (``/``).
HOME_SLUG = "index"


def resolve_page_path(path: str) -> Page | None:
    """Resolve a URL path to a Page by walking the ``parent``/``slug`` tree.

    ``path`` is the request path without the leading slash (e.g. ``"blog/my-post"``).
    Each segment matches a child ``slug`` under the previously matched page, starting
    from the top level (``parent=None``). The empty path resolves to the top-level
    :data:`HOME_SLUG` page. Returns ``None`` when no page matches.
    """
    segments = [segment for segment in path.strip("/").split("/") if segment]
    if not segments:
        segments = [HOME_SLUG]
    page: Page | None = None
    parent_id = None
    for segment in segments:
        page = Page.objects.filter(parent_id=parent_id, slug=segment).first()
        if page is None:
            return None
        parent_id = page.pk
    return page


def list_pages() -> list[Page]:
    """Return all pages."""
    return list(Page.objects.all())


def list_elements() -> list[Element]:
    """Return all elements (ordered by position, then creation)."""
    return list(Element.objects.all())


# Elements --------------------------------------------------------------------
def create_element(
    kind: str,
    *,
    parent: Element | None = None,
    position: int = 0,
    content: str = "",
    parameters: dict | None = None,
    is_reusable: bool = False,
) -> Element:
    """Create an element."""
    return Element.objects.create(
        kind=kind,
        parent=parent,
        position=position,
        content=content,
        parameters=parameters or {},
        is_reusable=is_reusable,
    )


def update_element(
    element: Element,
    *,
    kind: str | None = None,
    content: str | None = None,
    parameters: dict | None = None,
    is_reusable: bool | None = None,
) -> Element:
    """Update an element's fields (``None`` leaves a field unchanged)."""
    if kind is not None:
        element.kind = kind
    if content is not None:
        element.content = content
    if parameters is not None:
        element.parameters = parameters
    if is_reusable is not None:
        element.is_reusable = is_reusable
    element.save()
    return element


def set_element_content(element: Element, content: str) -> Element:
    """Replace an element's text/HTML content."""
    element.content = content
    element.save()
    return element


def move_element(element: Element, *, parent: Element | None, position: int) -> Element:
    """Move an element to a new parent and position."""
    element.parent = parent
    element.position = position
    element.save()
    return element


def reorder_children(parent: Element, ordered_ids: Iterable[UUID]) -> None:
    """Set children positions to match the given order (saving each, so caches invalidate)."""
    children = {child.pk: child for child in parent.children.all()}
    for position, element_id in enumerate(ordered_ids):
        child = children.get(element_id)
        if child is not None:
            child.position = position
            child.save()


def delete_element(element: Element) -> None:
    """Delete an element (and its subtree, via cascade)."""
    element.delete()


# Settings --------------------------------------------------------------------
def set_setting(key: str, value: object) -> Setting:
    """Create or update a setting."""
    setting, _ = Setting.objects.update_or_create(key=key, defaults={"value": value})
    return setting


def get_setting(key: str, default: object = None) -> object:
    """Return a setting's value, or ``default`` if it is not set."""
    try:
        return Setting.objects.get(key=key).value
    except Setting.DoesNotExist:
        return default


def get_settings() -> dict[str, object]:
    """Return all settings as a ``key -> value`` mapping."""
    return {setting.key: setting.value for setting in Setting.objects.all()}


# Assets ----------------------------------------------------------------------
def list_assets() -> list[Asset]:
    """Return all assets."""
    return list(Asset.objects.all())


def upload_asset(name: str, content_type: str, data: bytes) -> Asset:
    """Store an asset, keeping its bytes in Postgres or an active storage backend.

    The sha256 content hash is computed from ``data`` here. With no backend the bytes
    go in ``Asset.data``; with a backend active they are written to it under the hash
    (content-addressed, ARCHITECTURE.md §9) and only ``storage_key`` is kept on the row.
    """
    sha256 = hashlib.sha256(data).hexdigest()
    backend = get_storage_backend()
    if backend is None:
        return Asset.objects.create(name=name, content_type=content_type, data=data, sha256=sha256)
    backend.write(sha256, data, content_type)
    return Asset.objects.create(name=name, content_type=content_type, data=b"", storage_key=sha256, sha256=sha256)
