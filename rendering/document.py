"""The HTML document wrapper around a page's rendered element tree."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils.html import format_html
from django.utils.safestring import mark_safe

if TYPE_CHECKING:
    from django.utils.safestring import SafeString

    from content.models import Page
    from rendering.sources import ContentSource


def render_document(page: Page, body: SafeString, source: ContentSource) -> SafeString:
    """Wrap a page's rendered element tree in a minimal, valid HTML document.

    The ``<head>`` is data like everything else: the tenant-wide ``site.head_html``
    setting (stylesheet links, favicon, scripts) plus the page's optional
    ``head_html`` parameter (meta description and the like) are injected verbatim.
    Both are editor-trusted HTML — the head-side counterpart of the ``raw_html``
    block (§6, §8). The setting is read from ``source``, so a published page gets its
    revision's head, not the working copy's (§16). The document language is the
    page's ``lang`` parameter, else the tenant-wide ``site.lang`` setting, else ``en``.
    """
    lang = page.parameters.get("lang") or source.setting("site.lang", "") or "en"
    head = mark_safe(str(source.setting("site.head_html", "")) + str(page.parameters.get("head_html", "")))  # noqa: S308 - editor-trusted head HTML
    return format_html(
        '<!DOCTYPE html>\n<html lang="{0}">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>{1}</title>\n{2}</head>\n<body>{3}</body>\n</html>\n",
        lang,
        page.name,
        head,
        body,
    )
