"""Collections: ``page_list`` listings, RSS feeds, sitemap.xml, robots.txt.

A blog is a page whose child pages are posts (``parameters``: ``date``, ``summary``,
``tags``). A ``page_list`` element lists them; adding or editing a post refreshes every
cached listing; ``<blog>/feed.xml`` serves them as RSS; ``/sitemap.xml`` lists every page.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest
from django.core.cache import cache
from django.test import Client
from django_tenants.utils import tenant_context
from lxml import html

from caching.keys import fragment_cache_key
from caching.service import render_fragment_cached
from common.tenancy import tenant_connection
from content.models import Element, Page
from services import content as svc
from services import publishing
from tenants.models import Domain, Tenant


def _blog(schema: str, **list_parameters: object) -> tuple[Tenant, dict[str, str], Page]:
    """Create a tenant with a home page and a ``/blog`` page listing its child posts."""
    tenant_connection().set_schema_to_public()  # a previous request leaves the connection in its tenant
    tenant = Tenant.objects.create(schema_name=schema, name=schema)
    domain = f"{schema.replace('_', '-')}.localhost"
    Domain.objects.create(domain=domain, tenant=tenant, is_primary=True)
    with tenant_context(tenant):
        svc.create_page("index", "Home", root_element=svc.create_element("text", content="home"))
        blog = svc.create_page("blog", "Blog", parameters={"summary": "News from us"})
        listing = svc.create_element("page_list", parameters={"parent_id": str(blog.pk), **list_parameters})
        svc.set_page_root_element(blog, listing)
        for slug, name, date, tags in [
            ("first", "First post", "2026-01-10", ["news"]),
            ("third", "Third post", "2026-03-05", ["news", "release"]),
            ("second", "Second post", "2026-02-20", []),
        ]:
            svc.create_page(slug, name, parent=blog, parameters={"date": date, "summary": f"About {slug}", "tags": tags})
    return tenant, {"host": domain}, blog


def _items(body: str) -> list[str]:
    """Return the link texts of the listing on a page."""
    return [link.text_content() for link in html.fromstring(body).xpath("//ul/li/a")]


def _get(path: str, host: dict[str, str]) -> str:
    response = Client().get(path, headers=host)
    assert response.status_code == 200, response.status_code
    return response.content.decode()


@pytest.mark.django_db(transaction=True)
def test_page_list_shows_children_newest_first_with_dates_and_summaries() -> None:
    _, host, _ = _blog("coll_list")
    body = _get("/blog", host)

    assert _items(body) == ["Third post", "Second post", "First post"]
    item = html.fromstring(body).xpath("//ul/li")[0]
    assert item.xpath("a")[0].get("href") == "/blog/third"
    assert item.xpath("time")[0].get("datetime") == "2026-03-05"
    assert item.xpath("p")[0].text_content() == "About third"


@pytest.mark.django_db(transaction=True)
def test_page_list_filters_by_tag_limits_and_orders_by_position() -> None:
    _, host, _ = _blog("coll_tag", tag="news", limit=1)
    assert _items(_get("/blog", host)) == ["Third post"]

    _, host, _ = _blog("coll_order", order="position")
    assert _items(_get("/blog", host)) == ["First post", "Second post", "Third post"]


@pytest.mark.django_db(transaction=True)
def test_adding_editing_moving_and_deleting_posts_refresh_the_cached_listing() -> None:
    tenant, host, blog = _blog("coll_inval")
    assert len(_items(_get("/blog", host))) == 3  # warm the cache

    with tenant_context(tenant):
        svc.create_page("fourth", "Fourth post", parent=blog, parameters={"date": "2026-04-01"})
    assert _items(_get("/blog", host))[0] == "Fourth post"

    with tenant_context(tenant):
        svc.update_page(Page.objects.get(slug="first"), name="First post (edited)")
    assert "First post (edited)" in _items(_get("/blog", host))

    with tenant_context(tenant):
        second = Page.objects.get(slug="second")
        second.parent = None  # moved out of the blog
        second.save()
    assert "Second post" not in _items(_get("/blog", host))

    with tenant_context(tenant):
        svc.delete_page(Page.objects.get(slug="third"))
    assert _items(_get("/blog", host)) == ["Fourth post", "First post (edited)"]


@pytest.mark.django_db(transaction=True)
def test_a_page_list_fragment_refreshes_when_a_post_is_added() -> None:
    tenant, _, blog = _blog("coll_frag")
    with tenant_context(tenant):
        listing = Element.objects.get(kind="page_list")
        render_fragment_cached(listing)
        svc.create_page("fourth", "Fourth post", parent=blog, parameters={"date": "2026-04-01"})
        assert cache.get(fragment_cache_key(listing.pk)) is None
        assert "Fourth post" in render_fragment_cached(listing)


@pytest.mark.django_db(transaction=True)
def test_published_listing_ignores_draft_posts_until_publish() -> None:
    tenant, host, blog = _blog("coll_pub")
    with tenant_context(tenant):
        publishing.publish()
        svc.create_page("draft", "Draft post", parent=blog, parameters={"date": "2026-09-09"})
    assert "Draft post" not in _items(_get("/blog", host))
    assert "Draft post" not in _get("/blog/feed.xml", host)

    with tenant_context(tenant):
        publishing.publish()
    assert _items(_get("/blog", host))[0] == "Draft post"
    assert "Draft post" in _get("/blog/feed.xml", host)


@pytest.mark.django_db(transaction=True)
def test_rss_feed_lists_posts_with_absolute_links_and_dates() -> None:
    _, host, _ = _blog("coll_rss")
    response = Client().get("/blog/feed.xml", headers=host)
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/rss+xml")

    channel = ET.fromstring(response.content).find("channel")  # noqa: S314 - our own response, not untrusted input
    assert channel is not None
    assert channel.findtext("title") == "Blog"
    assert channel.findtext("description") == "News from us"
    items = channel.findall("item")
    assert [item.findtext("title") for item in items] == ["Third post", "Second post", "First post"]
    assert items[0].findtext("link") == "http://coll-rss.localhost/blog/third"
    assert items[0].findtext("pubDate", "").startswith("Thu, 05 Mar 2026")
    assert [c.text for c in items[0].findall("category")] == ["news", "release"]

    assert Client().get("/nope/feed.xml", headers=host).status_code == 404


@pytest.mark.django_db(transaction=True)
def test_sitemap_lists_public_pages_except_noindex() -> None:
    tenant, host, _ = _blog("coll_sitemap")
    with tenant_context(tenant):
        svc.create_page("secret", "Secret", parameters={"noindex": True})

    root = ET.fromstring(Client().get("/sitemap.xml", headers=host).content)  # noqa: S314 - our own response, not untrusted input
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [loc.text for loc in root.findall("s:url/s:loc", ns)]
    base = "http://coll-sitemap.localhost"
    assert locs == [f"{base}/", f"{base}/blog", f"{base}/blog/first", f"{base}/blog/second", f"{base}/blog/third"]
    assert root.findtext("s:url[s:loc='" + f"{base}/blog/third" + "']/s:lastmod", namespaces=ns) == "2026-03-05"


@pytest.mark.django_db(transaction=True)
def test_robots_txt_defaults_to_the_sitemap_and_can_be_overridden() -> None:
    tenant, host, _ = _blog("coll_robots")
    body = _get("/robots.txt", host)
    assert "Disallow: /_preview/" in body
    assert "Sitemap: http://coll-robots.localhost/sitemap.xml" in body

    with tenant_context(tenant):
        svc.set_setting("site.robots_txt", "User-agent: *\nDisallow: /")
    assert _get("/robots.txt", host) == "User-agent: *\nDisallow: /"
