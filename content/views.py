"""Public content-serving views.

Assets live in the database (ARCHITECTURE.md §9) but are served like static files:
content-hashed URLs with a long, immutable cache so the browser and CDNs cache them
freely — a change is simply a new hash, hence a new URL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import quote

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse, HttpResponseNotModified
from django.utils.feedgenerator import Rss201rev2Feed
from django.utils.html import format_html
from django.views.decorators.clickjacking import xframe_options_sameorigin

from caching.service import render_fragment_cached, render_page_cached, render_published_document, render_published_fragment
from common.users import request_user
from content.models import Asset, Element
from rendering.blocks import collection_pages
from rendering.document import render_document
from rendering.sources import DatabaseSource
from services.content import resolve_page_path
from services.publishing import live_revision_id, snapshot_source

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest

    from rendering.sources import ContentSource

ASSET_MAX_AGE = 31_536_000  # one year
#: Most recent entries an RSS feed carries.
FEED_MAX_ITEMS = 50


def serve_asset(request: HttpRequest, sha256: str, name: str | None = None) -> HttpResponse:
    """Serve a stored asset by content hash with an immutable, year-long cache."""
    asset = Asset.objects.filter(sha256=sha256).first()
    if asset is None:
        raise Http404
    etag = f'"{asset.sha256}"'
    if request.headers.get("If-None-Match") == etag:
        return HttpResponseNotModified()
    response = HttpResponse(asset.read_bytes(), content_type=asset.content_type)
    response["Cache-Control"] = f"public, max-age={ASSET_MAX_AGE}, immutable"
    response["ETag"] = etag
    return response


def asset_url(asset: Asset) -> str:
    """Build the content-hashed URL for an asset (for embedding in rendered pages)."""
    return f"/_assets/{asset.sha256}/{quote(str(asset.name))}"


def serve_fragment(request: HttpRequest, element_id: UUID) -> HttpResponse:
    """Serve a rendered element subtree as an HTML fragment for htmx to swap in.

    Public, so in draft mode it serves the live revision's version of the element —
    never the unpublished working copy (§16).
    """
    revision_id = live_revision_id()
    if revision_id is not None:
        html = render_published_fragment(revision_id, element_id)
        if html is None:
            raise Http404
        return HttpResponse(html, content_type="text/html")
    element = Element.objects.filter(pk=element_id).first()
    if element is None:
        raise Http404
    return HttpResponse(render_fragment_cached(element), content_type="text/html")


def serve_page(request: HttpRequest, path: str = "") -> HttpResponse:
    """Resolve a URL path to a Page in the current tenant and serve its rendered HTML.

    The tenant is already selected by ``TenantMainMiddleware``; the path is walked
    down the ``parent``/``slug`` tree (``/`` → the home page). In draft mode the page
    comes from the live revision (§16); in live mode (never published) from the
    working copy, through the dependency-invalidated page cache (§7).
    """
    revision_id = live_revision_id()
    if revision_id is not None:
        html = render_published_document(revision_id, path)
        if html is None:
            raise Http404
        return HttpResponse(html, content_type="text/html")
    return _serve_working_copy(path)


@xframe_options_sameorigin  # the studio frames the preview
def serve_preview(request: HttpRequest, path: str = "") -> HttpResponse:
    """Serve the unpublished working copy at ``/_preview/<path>`` to signed-in editors.

    Same renderers as the public site, so what an editor previews is exactly what
    publishing will put live. Anonymous visitors are sent to the admin login; the
    response is never cached by intermediaries nor indexed.
    """
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    if not request_user(request).has_perm("content.view_page"):
        raise PermissionDenied
    response = _serve_working_copy(path)
    response["Cache-Control"] = "private, no-store"
    response["X-Robots-Tag"] = "noindex"
    return response


def _serve_working_copy(path: str) -> HttpResponse:
    """Render the working copy's page at ``path`` as a full document (404 if absent)."""
    page = resolve_page_path(path)
    if page is None:
        raise Http404
    return HttpResponse(render_document(page, render_page_cached(page), DatabaseSource()), content_type="text/html")


def _public_source() -> ContentSource:
    """Return what visitors see: the live revision in draft mode, else the working copy."""
    revision_id = live_revision_id()
    return snapshot_source(revision_id) if revision_id is not None else DatabaseSource()


def serve_feed(request: HttpRequest, path: str) -> HttpResponse:
    """Serve ``<collection path>/feed.xml``: an RSS 2.0 feed of the page's children.

    Items are ordered and described like a ``page_list`` (newest ``date`` first, the
    ``summary`` as description). The channel description is the page's ``summary``.
    """
    source = _public_source()
    page = source.resolve_path(path)
    if page is None:
        raise Http404
    feed = Rss201rev2Feed(
        title=page.name,
        link=request.build_absolute_uri(source.page_path(page)),
        description=str(page.parameters.get("summary", "")),
        feed_url=request.build_absolute_uri(),
        language=str(page.parameters.get("lang", "en")),
    )
    for item in collection_pages(source, page.pk)[:FEED_MAX_ITEMS]:
        link = request.build_absolute_uri(source.page_path(item))
        feed.add_item(
            title=item.name,
            link=link,
            unique_id=link,
            description=str(item.parameters.get("summary", "")),
            pubdate=_parse_date(item.parameters.get("date")),
            categories=[str(tag) for tag in item.parameters.get("tags") or []],
        )
    return HttpResponse(feed.writeString("utf-8"), content_type="application/rss+xml; charset=utf-8")


def serve_sitemap(request: HttpRequest) -> HttpResponse:
    """Serve ``/sitemap.xml``: every public page (minus ``parameters["noindex"]`` ones)."""
    source = _public_source()
    entries = []
    for page in sorted(source.pages(), key=source.page_path):
        if page.parameters.get("noindex"):
            continue
        lastmod = _parse_date(page.parameters.get("date"))
        entries.append(
            format_html(
                "<url><loc>{0}</loc>{1}</url>",
                request.build_absolute_uri(source.page_path(page)),
                format_html("<lastmod>{0}</lastmod>", lastmod.date().isoformat()) if lastmod else "",
            ),
        )
    body = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(entries) + "</urlset>\n"
    return HttpResponse(body, content_type="application/xml; charset=utf-8")


def serve_robots(request: HttpRequest) -> HttpResponse:
    """Serve ``/robots.txt``: the ``site.robots_txt`` setting, else allow-all plus the sitemap."""
    custom = _public_source().setting("site.robots_txt")
    if custom:
        return HttpResponse(str(custom), content_type="text/plain; charset=utf-8")
    body = f"User-agent: *\nDisallow: /_preview/\nSitemap: {request.build_absolute_uri('/sitemap.xml')}\n"
    return HttpResponse(body, content_type="text/plain; charset=utf-8")


def _parse_date(value: object) -> datetime | None:
    """Parse a page's ISO 8601 ``date`` parameter (date-only means midnight UTC); ``None`` if invalid."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
