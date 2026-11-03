"""Built-in element renderers (block kinds).

Editor-supplied text is escaped here (no XSS); ``raw_html`` is the single,
explicit trusted-HTML escape hatch noted in ARCHITECTURE.md §6.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe

from rendering.registry import register, render_element

if TYPE_CHECKING:
    from content.models import Element, Page
    from rendering.registry import RenderContext
    from rendering.sources import ContentSource


def _joined(children: list[SafeString]) -> SafeString:
    """Concatenate already-escaped child fragments."""
    return mark_safe("".join(children))  # noqa: S308 - children are already escaped SafeStrings


def _tagged(tag: str, inner: str | SafeString, css: object) -> SafeString:
    """Wrap ``inner`` in ``tag`` with an optional class attribute; non-safe inner is escaped."""
    if css:
        return format_html('<{0} class="{1}">{2}</{0}>', tag, css, inner)
    return format_html("<{0}>{1}</{0}>", tag, inner)


@register("container")
def render_container(element: Element, children: list[SafeString], ctx: RenderContext) -> SafeString:
    """Wrap already-rendered children in a configurable container tag."""
    return _tagged(element.parameters.get("tag", "div"), _joined(children), element.parameters.get("class"))


@register("text")
def render_text(element: Element, children: list[SafeString], ctx: RenderContext) -> SafeString:
    """Render escaped plain text as a paragraph."""
    return format_html("<p>{0}</p>", element.content)


@register("heading")
def render_heading(element: Element, children: list[SafeString], ctx: RenderContext) -> SafeString:
    """Render an escaped heading at the configured level (default 1)."""
    level = element.parameters.get("level", 1)
    return format_html("<h{0}>{1}</h{0}>", level, element.content)


@register("image")
def render_image(element: Element, children: list[SafeString], ctx: RenderContext) -> SafeString:
    """Render an ``<img>`` from a DB asset (``parameters["asset_id"]``) or an external ``src``.

    An asset id resolves to the asset's immutable content-hashed URL (§9) at render
    time; ``alt`` defaults to empty (a decorative image). Like ``reference``, a
    dangling or malformed asset id renders as nothing.
    """
    from content.models import Asset  # noqa: PLC0415 - avoid import cycle rendering <- content
    from content.views import asset_url  # noqa: PLC0415 - avoid import cycle rendering <- content

    src = element.parameters.get("src", "")
    asset_id = str(element.parameters.get("asset_id") or "")
    if asset_id:
        try:
            asset = Asset.objects.filter(pk=asset_id).first()
        except ValueError, ValidationError:
            return SafeString("")
        if asset is None:
            return SafeString("")
        src = asset_url(asset)
    if not src:
        return SafeString("")
    alt = element.parameters.get("alt", "")
    css = element.parameters.get("class")
    if css:
        return format_html('<img src="{0}" alt="{1}" class="{2}">', src, alt, css)
    return format_html('<img src="{0}" alt="{1}">', src, alt)


@register("link")
def render_link(element: Element, children: list[SafeString], ctx: RenderContext) -> SafeString:
    """Render an escaped hyperlink: the label is the nested children if present, else the element's content.

    A missing ``href`` renders as nothing, matching the dangling-reference behaviour.
    A "button" is this kind with a class — no separate element kind needed.
    """
    href = element.parameters.get("href", "")
    if not href:
        return SafeString("")
    label = _joined(children) if children else element.content
    css = element.parameters.get("class")
    if css:
        return format_html('<a href="{0}" class="{1}">{2}</a>', href, css, label)
    return format_html('<a href="{0}">{1}</a>', href, label)


@register("list")
def render_list(element: Element, children: list[SafeString], ctx: RenderContext) -> SafeString:
    """Render children as a list — ``<ul>``, or ``<ol>`` when ``parameters["ordered"]`` is set."""
    tag = "ol" if element.parameters.get("ordered") else "ul"
    return _tagged(tag, _joined(children), element.parameters.get("class"))


@register("list_item")
def render_list_item(element: Element, children: list[SafeString], ctx: RenderContext) -> SafeString:
    """Render an ``<li>``: the nested children if present, else the element's escaped content."""
    return _tagged("li", _joined(children) if children else element.content, element.parameters.get("class"))


@register("nav")
def render_nav(element: Element, children: list[SafeString], ctx: RenderContext) -> SafeString:
    """Render a ``<nav>`` landmark wrapping its children (typically a ``list`` of ``link`` items)."""
    return _tagged("nav", _joined(children), element.parameters.get("class"))


@register("raw_html")
def render_raw_html(element: Element, children: list[SafeString], ctx: RenderContext) -> SafeString:
    """Output trusted raw HTML verbatim (explicit escape hatch — see §6)."""
    return mark_safe(element.content)  # noqa: S308 - explicit trusted-HTML block


@register("reference")
def render_reference(element: Element, children: list[SafeString], ctx: RenderContext) -> SafeString:
    """Render another element's subtree in place — the include mechanism for reusable elements.

    ``parameters["element_id"]`` names the target (a shared header/footer, typically its
    own tree root with ``is_reusable=True``). The target renders through the *same*
    context, so its elements join the page's dependency set and editing the shared
    element invalidates every page that references it (§7). Dangling or malformed
    references render as nothing; cyclic references are cut rather than recursed.
    """
    target_id = str(element.parameters.get("element_id") or "")
    if not target_id or target_id in ctx.active_references:
        return SafeString("")
    target = ctx.source.element(target_id)
    if target is None:
        return SafeString("")
    ctx.active_references.add(target_id)
    try:
        return render_element(target, ctx)
    finally:
        ctx.active_references.discard(target_id)


@register("page_list")
def render_page_list(element: Element, children: list[SafeString], ctx: RenderContext) -> SafeString:
    """Render a collection: links to the child pages of ``parameters["parent_id"]`` (a blog, news, docs...).

    Each item is the child page's name linking to its URL, plus its ``parameters["date"]``
    (ISO 8601, shown in a ``<time>``) and ``parameters["summary"]`` when present. Options:
    ``order`` — ``"date"`` (newest first, the default) or ``"position"`` (tree order);
    ``tag`` — keep only pages whose ``parameters["tags"]`` contains it; ``limit`` — at
    most this many items; ``class`` — on the ``<ul>``. Adding, editing, or removing a
    child page refreshes every listing of it (the collection is a render dependency).
    The same children feed ``<page path>/feed.xml`` (RSS). A missing parent renders nothing.
    """
    parent_id = str(element.parameters.get("parent_id") or "")
    parent = ctx.source.page(parent_id) if parent_id else None
    if parent is None:
        return SafeString("")
    ctx.record_collection(parent.pk)
    pages = collection_pages(ctx.source, parent.pk, order=element.parameters.get("order", "date"), tag=element.parameters.get("tag"))
    limit = element.parameters.get("limit")
    if isinstance(limit, int) and limit >= 0:
        pages = pages[:limit]
    items = [_page_list_item(page, ctx) for page in pages]
    return _tagged("ul", _joined(items), element.parameters.get("class"))


def collection_pages(source: ContentSource, parent_id: object, *, order: str = "date", tag: object = None) -> list[Page]:
    """Return a collection's child pages, filtered by ``tag`` and sorted (shared with the RSS feed)."""
    pages = source.child_pages(parent_id)
    if tag:
        pages = [page for page in pages if tag in (page.parameters.get("tags") or [])]
    if order == "date":
        # Undated pages sort last; the sort is stable, so ties keep tree order.
        pages = sorted(pages, key=lambda page: str(page.parameters.get("date") or ""), reverse=True)
    return pages


def _page_list_item(page: Page, ctx: RenderContext) -> SafeString:
    """Render one collection entry: link, optional date, optional summary."""
    parts = [format_html('<a href="{0}">{1}</a>', ctx.source.page_path(page), page.name)]
    date = page.parameters.get("date")
    if date:
        parts.append(format_html(' <time datetime="{0}">{0}</time>', str(date)[:10]))
    summary = page.parameters.get("summary")
    if summary:
        parts.append(format_html("<p>{0}</p>", summary))
    return format_html("<li>{0}</li>", _joined(parts))
