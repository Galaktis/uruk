"""Renderer registry and element-tree rendering (ARCHITECTURE.md §6).

A website needs no committed templates: each element ``kind`` maps to a generic,
committed renderer, and every site is composed at runtime from element data.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.utils.safestring import SafeString

from rendering.sources import ContentSource, DatabaseSource

if TYPE_CHECKING:
    from uuid import UUID

    from content.models import Element, Page

Renderer = Callable[["Element", list[SafeString], "RenderContext"], SafeString]

RENDERERS: dict[str, Renderer] = {}


class UnknownElementKindError(KeyError):
    """Raised when an element's ``kind`` has no registered renderer."""


def register(kind: str) -> Callable[[Renderer], Renderer]:
    """Register a renderer for the given element ``kind``."""

    def decorator(func: Renderer) -> Renderer:
        RENDERERS[kind] = func
        return func

    return decorator


@dataclass
class RenderContext:
    """Per-render state; collects the element dependency set for cache invalidation (§7).

    ``source`` is where the render reads content from: the working copy by default, or a
    published revision's snapshot (§16).
    """

    source: ContentSource = field(default_factory=DatabaseSource)
    dependencies: set[UUID] = field(default_factory=set)
    #: Ids of ``reference`` targets currently being rendered, so cyclic references
    #: are cut instead of recursing forever (a diamond — the same target referenced
    #: twice on one page — is fine, because ids are removed once rendered).
    active_references: set[str] = field(default_factory=set)

    def record_dependency(self, element: Element) -> None:
        """Record that the rendered output depends on this element."""
        self.dependencies.add(element.pk)

    def record_collection(self, parent_page_id: UUID) -> None:
        """Record that the output lists this page's children (a collection, e.g. blog posts).

        Stored in the same dependency set as element ids — UUIDv7 ids never collide — so
        saving or deleting any child page evicts the listing pages (§7).
        """
        self.dependencies.add(parent_page_id)


def render_element(element: Element, ctx: RenderContext) -> SafeString:
    """Recursively render an element and its ordered children to safe HTML."""
    ctx.record_dependency(element)
    children = [render_element(child, ctx) for child in ctx.source.children(element)]
    try:
        renderer = RENDERERS[element.kind]
    except KeyError as exc:
        msg = f"No renderer registered for element kind {element.kind!r}"
        raise UnknownElementKindError(msg) from exc
    return renderer(element, children, ctx)


def render_page(page: Page, ctx: RenderContext | None = None) -> SafeString:
    """Render a page from its root element tree (empty string if it has none)."""
    ctx = ctx if ctx is not None else RenderContext()
    root = ctx.source.element(page.root_element_id) if page.root_element_id else None
    if root is None:
        return SafeString("")
    return render_element(root, ctx)
