"""Content sources: where a render reads its element tree, pages, and settings from.

The renderers never query content directly; they go through the ``source`` on the
:class:`~rendering.registry.RenderContext`. That one seam lets the same committed
renderers draw either the **working copy** (the live DB rows — :class:`DatabaseSource`)
or a **published revision** (an immutable in-memory snapshot — :class:`SnapshotSource`),
so the public site and the draft preview are guaranteed to render identically
(ARCHITECTURE.md §16).
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from django.core.exceptions import ValidationError

if TYPE_CHECKING:
    from content.models import Element, Page


class ContentSource(Protocol):
    """What a render needs to read: tree structure, include targets, pages, settings."""

    def children(self, element: Element) -> list[Element]:
        """Return the element's children in render order."""
        ...

    def element(self, element_id: object) -> Element | None:
        """Return the element with this id, or ``None`` if it is absent or malformed."""
        ...

    def resolve_path(self, path: str) -> Page | None:
        """Resolve a URL path (no leading slash) to a page, or ``None``."""
        ...

    def setting(self, key: str, default: object = None) -> object:
        """Return a setting's value, or ``default``."""
        ...

    def pages(self) -> list[Page]:
        """Return every page."""
        ...

    def page(self, page_id: object) -> Page | None:
        """Return the page with this id, or ``None`` if it is absent or malformed."""
        ...

    def child_pages(self, parent_id: object) -> list[Page]:
        """Return a page's direct children in tree order (``[]`` for an unknown/malformed id)."""
        ...

    def page_path(self, page: Page) -> str:
        """Return the URL path a page is served at (``/`` for the home page)."""
        ...


class DatabaseSource:
    """The working copy: reads straight from the tenant's Element/Page/Setting rows."""

    def children(self, element: Element) -> list[Element]:
        """Return the element's children from the DB (model ordering: position, created)."""
        return list(element.children.all())

    def element(self, element_id: object) -> Element | None:
        """Fetch an element by id; malformed ids resolve to ``None``."""
        from content.models import Element as ElementModel  # noqa: PLC0415 - avoid import cycle rendering <- content

        try:
            return ElementModel.objects.filter(pk=element_id).first()
        except ValueError, ValidationError:
            return None

    def resolve_path(self, path: str) -> Page | None:
        """Walk the DB page tree for ``path``."""
        from services.content import resolve_page_path  # noqa: PLC0415 - avoid import cycle rendering <- services

        return resolve_page_path(path)

    def setting(self, key: str, default: object = None) -> object:
        """Read a setting row."""
        from services.content import get_setting  # noqa: PLC0415 - avoid import cycle rendering <- services

        return get_setting(key, default)

    def pages(self) -> list[Page]:
        """Return every page row."""
        from content.models import Page as PageModel  # noqa: PLC0415 - avoid import cycle rendering <- content

        return list(PageModel.objects.all())

    def page(self, page_id: object) -> Page | None:
        """Fetch a page by id; malformed ids resolve to ``None``."""
        from content.models import Page as PageModel  # noqa: PLC0415 - avoid import cycle rendering <- content

        try:
            return PageModel.objects.filter(pk=page_id).first()
        except ValueError, ValidationError:
            return None

    def child_pages(self, parent_id: object) -> list[Page]:
        """Return a page's children from the DB (model ordering: position, slug)."""
        from content.models import Page as PageModel  # noqa: PLC0415 - avoid import cycle rendering <- content

        try:
            return list(PageModel.objects.filter(parent_id=parent_id))
        except ValueError, ValidationError:
            return []

    def page_path(self, page: Page) -> str:
        """Build the path by walking the parent chain in the DB."""
        from content.models import Page as PageModel  # noqa: PLC0415 - avoid import cycle rendering <- content

        slugs = [page.slug]
        parent_id = page.parent_id
        while parent_id is not None:
            parent = PageModel.objects.filter(pk=parent_id).only("slug", "parent_id").first()
            if parent is None:
                break
            slugs.append(parent.slug)
            parent_id = parent.parent_id
        return _path(slugs)


class SnapshotSource:
    """A published revision: unsaved model instances rebuilt from snapshot records.

    Built once per revision and then only read, so one instance can be shared across
    requests and threads. Children are ordered by ``(position, id)`` — UUIDv7 ids are
    time-ordered, which reproduces the model's ``(position, created)`` ordering.
    """

    def __init__(self, data: dict) -> None:
        """Index the snapshot's elements, pages, and settings for O(1) lookups."""
        from content.models import Element as ElementModel  # noqa: PLC0415 - avoid import cycle rendering <- content
        from content.models import Page as PageModel  # noqa: PLC0415 - avoid import cycle rendering <- content

        self._elements: dict[UUID, Element] = {}
        children: dict[UUID | None, list[Element]] = defaultdict(list)
        for record in data["elements"]:
            element = ElementModel(
                id=UUID(record["id"]),
                kind=record["kind"],
                parent_id=_uuid_or_none(record["parent_id"]),
                position=record["position"],
                content=record["content"],
                parameters=record["parameters"],
                is_reusable=record["is_reusable"],
            )
            self._elements[element.pk] = element
            children[element.parent_id].append(element)
        self._children = {parent_id: sorted(rows, key=lambda e: (e.position, e.pk)) for parent_id, rows in children.items()}

        self._pages: dict[tuple[UUID | None, str], Page] = {}
        self._pages_by_id: dict[UUID, Page] = {}
        page_children: dict[UUID | None, list[Page]] = defaultdict(list)
        for record in data["pages"]:
            page = PageModel(
                id=UUID(record["id"]),
                slug=record["slug"],
                name=record["name"],
                parent_id=_uuid_or_none(record["parent_id"]),
                position=record["position"],
                root_element_id=_uuid_or_none(record["root_element_id"]),
                parameters=record["parameters"],
            )
            self._pages[page.parent_id, page.slug] = page
            self._pages_by_id[page.pk] = page
            page_children[page.parent_id].append(page)
        self._page_children = {parent_id: sorted(rows, key=lambda p: (p.position, p.slug)) for parent_id, rows in page_children.items()}
        self._settings = {record["key"]: record["value"] for record in data["settings"]}

    def children(self, element: Element) -> list[Element]:
        """Return the snapshot children of ``element``."""
        return self._children.get(element.pk, [])

    def element(self, element_id: object) -> Element | None:
        """Look an element up in the snapshot; malformed ids resolve to ``None``."""
        try:
            return self._elements.get(UUID(str(element_id)))
        except ValueError:
            return None

    def resolve_path(self, path: str) -> Page | None:
        """Walk the snapshot's page tree, mirroring :func:`services.content.resolve_page_path`."""
        from services.content import HOME_SLUG  # noqa: PLC0415 - avoid import cycle rendering <- services

        segments = [segment for segment in path.strip("/").split("/") if segment] or [HOME_SLUG]
        page: Page | None = None
        parent_id: UUID | None = None
        for segment in segments:
            page = self._pages.get((parent_id, segment))
            if page is None:
                return None
            parent_id = page.pk
        return page

    def setting(self, key: str, default: object = None) -> object:
        """Read a setting from the snapshot."""
        return self._settings.get(key, default)

    def pages(self) -> list[Page]:
        """Return every page in the snapshot."""
        return list(self._pages_by_id.values())

    def page(self, page_id: object) -> Page | None:
        """Look a page up in the snapshot; malformed ids resolve to ``None``."""
        try:
            return self._pages_by_id.get(UUID(str(page_id)))
        except ValueError:
            return None

    def child_pages(self, parent_id: object) -> list[Page]:
        """Return the snapshot children of a page."""
        try:
            return self._page_children.get(UUID(str(parent_id)), [])
        except ValueError:
            return []

    def page_path(self, page: Page) -> str:
        """Build the path from the snapshot's page index."""
        slugs = [page.slug]
        parent = self._pages_by_id.get(page.parent_id) if page.parent_id else None
        while parent is not None:
            slugs.append(parent.slug)
            parent = self._pages_by_id.get(parent.parent_id) if parent.parent_id else None
        return _path(slugs)


def _path(slugs_leaf_first: list[str]) -> str:
    """Join a leaf-to-root slug chain into a URL path; the top-level home page is ``/``."""
    from services.content import HOME_SLUG  # noqa: PLC0415 - avoid import cycle rendering <- services

    if slugs_leaf_first == [HOME_SLUG]:
        return "/"
    return "/" + "/".join(reversed(slugs_leaf_first))


def _uuid_or_none(value: str | None) -> UUID | None:
    """Parse an optional UUID string from a snapshot record."""
    return UUID(value) if value else None
