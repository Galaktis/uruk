"""Compose the default-site archive (``tenants/seed/default_site.json``) from source files.

The default tenant's website — the one any unmatched hostname serves — ships with the
repository as a portability archive (``services.portability.ARCHIVE_VERSION`` format).
This script is the *authoring tool* for that archive: it reads the CSS/JS/SVG/HTML
sources in ``scripts/default_site/``, wires them into element trees, pages, assets, and
settings, and writes the JSON. It needs no database and no Django — pure composition.

Run it after editing any source file::

    uv run python scripts/build_default_site.py

Ids are deterministic, UUIDv7-shaped values derived from stable names, so regenerating
the archive produces minimal diffs while still satisfying the "every PK is a UUIDv7"
convention (time-ordered layout, valid version/variant bits).
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from pathlib import Path

SRC = Path(__file__).parent / "default_site"
OUT = Path(__file__).parent.parent / "tenants" / "seed" / "default_site.json"

#: Fixed millisecond timestamp the deterministic v7 ids count from (2026-07-01T00:00:00Z).
BASE_MS = 1_782_864_000_000

ARCHIVE_VERSION = 1


def uuid7_for(name: str, seq: int) -> str:
    """Return a deterministic, UUIDv7-shaped id for a stable name.

    Layout matches RFC 9562 v7 (48-bit timestamp, version, variant); the random bits
    come from a sha256 of ``name`` so the id never changes between builds, and ``seq``
    offsets the timestamp so ids stay time-ordered in authoring order.
    """
    digest = hashlib.sha256(name.encode()).digest()
    rand_a = int.from_bytes(digest[:2]) & 0x0FFF
    rand_b = int.from_bytes(digest[2:10]) & 0x3FFF_FFFF_FFFF_FFFF
    value = (BASE_MS + seq) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return str(uuid.UUID(int=value))


def read(name: str) -> str:
    """Read a source file from ``scripts/default_site/`` as text."""
    return (SRC / name).read_text(encoding="utf-8")


class Site:
    """Accumulates elements, pages, assets, and settings, then emits the archive."""

    def __init__(self) -> None:
        """Start an empty archive."""
        self.elements: list[dict] = []
        self.pages: list[dict] = []
        self.assets: list[dict] = []
        self.settings: list[dict] = []
        self._seq = 0

    def _id(self, name: str) -> str:
        """Mint the deterministic id for ``name`` (authoring order sets time order)."""
        self._seq += 1
        return uuid7_for(name, self._seq)

    def element(
        self,
        name: str,
        kind: str,
        *,
        parent: str | None = None,
        position: int = 0,
        content: str = "",
        parameters: dict | None = None,
        is_reusable: bool = False,
    ) -> str:
        """Add an element record; returns its id for use as a parent or reference."""
        element_id = self._id(f"element:{name}")
        self.elements.append(
            {
                "id": element_id,
                "kind": kind,
                "parent_id": parent,
                "position": position,
                "content": content,
                "parameters": parameters or {},
                "is_reusable": is_reusable,
            },
        )
        return element_id

    def page(self, slug: str, name: str, *, root_element: str, description: str) -> str:
        """Add a top-level page record rendering from ``root_element``."""
        page_id = self._id(f"page:{slug}")
        self.pages.append(
            {
                "id": page_id,
                "slug": slug,
                "name": name,
                "parent_id": None,
                "position": len(self.pages),
                "root_element_id": root_element,
                "parameters": {"lang": "en", "head_html": f'<meta name="description" content="{description}">'},
            },
        )
        return page_id

    def asset(self, name: str, content_type: str, data: bytes) -> str:
        """Add an asset record; returns its immutable content-hashed URL."""
        sha256 = hashlib.sha256(data).hexdigest()
        self.assets.append(
            {
                "id": self._id(f"asset:{name}"),
                "name": name,
                "content_type": content_type,
                "sha256": sha256,
                "data_base64": base64.b64encode(data).decode(),
            },
        )
        return f"/_assets/{sha256}/{name}"

    def setting(self, key: str, value: object) -> None:
        """Add a setting record."""
        self.settings.append({"id": self._id(f"setting:{key}"), "key": key, "value": value})

    def archive(self) -> dict:
        """Return the finished archive in ``services.portability`` format."""
        return {
            "uruk_archive_version": ARCHIVE_VERSION,
            "tenant": {"name": "Uruk"},
            "elements": self.elements,
            "pages": self.pages,
            "assets": self.assets,
            "settings": self.settings,
        }


def add_standard_page(
    site: Site,
    *,
    slug: str,
    title: str,
    description: str,
    header: str,
    footer: str,
    sections: list[tuple[str, str]],
    main_class: str = "",
) -> None:
    """Add a page whose main content is raw HTML sections between the shared header and footer."""
    root = site.element(f"{slug}/root", "container", parameters={"tag": "div"})
    site.element(f"{slug}/header-ref", "reference", parent=root, position=0, parameters={"element_id": header})
    main_params = {"tag": "main"} | ({"class": main_class} if main_class else {})
    main = site.element(f"{slug}/main", "container", parent=root, position=1, parameters=main_params)
    for position, (section_name, html) in enumerate(sections):
        site.element(f"{slug}/{section_name}", "raw_html", parent=main, position=position, content=html)
    site.element(f"{slug}/footer-ref", "reference", parent=root, position=2, parameters={"element_id": footer})
    site.page(slug, title, root_element=root, description=description)


#: Header navigation: one structured ``link`` element per (label, href) row.
NAV_LINKS: list[tuple[str, str]] = [
    ("Manifesto", "/manifesto"),
    ("Architecture", "/architecture"),
    ("Quickstart", "/quickstart"),
    ("Configuration", "/configuration"),
    ("GitHub", "https://github.com/Galaktis/uruk"),
]

#: Footer link columns: column title → (label, href) rows.
FOOTER_COLUMNS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Pages",
        [
            ("Home", "/"),
            ("Manifesto", "/manifesto"),
            ("Architecture", "/architecture"),
            ("Quickstart", "/quickstart"),
            ("Configuration", "/configuration"),
        ],
    ),
    (
        "Source",
        [
            ("github.com/Galaktis/uruk", "https://github.com/Galaktis/uruk"),
            ("ARCHITECTURE.md", "https://github.com/Galaktis/uruk/blob/main/ARCHITECTURE.md"),
            ("README.md", "https://github.com/Galaktis/uruk/blob/main/README.md"),
        ],
    ),
]


def add_header(site: Site, *, logo_asset_id: str) -> str:
    """Compose the shared header from structured elements — the block vocabulary eating its own dogfood.

    The brand logo is an ``image`` element over a DB asset and the menu is a ``nav``
    of ``link`` elements: an agent retargets a link or swaps the logo by editing one
    element, instead of doing string surgery on a ``raw_html`` blob. Only the brand
    name and tagline spans stay raw (there is no ``span`` kind).
    """
    header = site.element("shared/header", "container", parameters={"tag": "header", "class": "site-head"}, is_reusable=True)
    wrap = site.element("shared/header/wrap", "container", parent=header, position=0, parameters={"tag": "div", "class": "wrap"})
    brand = site.element("shared/header/brand", "link", parent=wrap, position=0, parameters={"href": "/", "class": "brand"})
    site.element("shared/header/brand-glyph", "image", parent=brand, position=0, parameters={"asset_id": logo_asset_id, "alt": ""})
    site.element("shared/header/brand-name", "raw_html", parent=brand, position=1, content="<span>URUK</span>")
    nav = site.element("shared/header/nav", "nav", parent=wrap, position=1, parameters={"class": "site-nav"})
    for position, (label, href) in enumerate(NAV_LINKS):
        site.element(f"shared/header/nav/{label.lower()}", "link", parent=nav, position=position, content=label, parameters={"href": href})
    site.element("shared/header/tag", "raw_html", parent=wrap, position=2, content='<span class="head-tag">a website is data</span>')
    return header


def add_footer(site: Site) -> str:
    """Compose the shared footer: heading/list/link columns as structure, prose blocks as raw HTML.

    The KI glyph block stays ``raw_html`` deliberately — its inline SVG uses
    ``currentColor``, which an ``image`` over an external asset cannot inherit.
    """
    footer = site.element("shared/footer", "container", parameters={"tag": "footer", "class": "site-foot"}, is_reusable=True)
    wrap = site.element("shared/footer/wrap", "container", parent=footer, position=0, parameters={"tag": "div", "class": "wrap"})
    site.element("shared/footer/badge", "raw_html", parent=wrap, position=0, content=read("foot-badge.html"))
    grid = site.element("shared/footer/grid", "container", parent=wrap, position=1, parameters={"tag": "div", "class": "foot-grid"})
    site.element("shared/footer/ki", "raw_html", parent=grid, position=0, content=read("foot-ki.html"))
    for column_index, (title, links) in enumerate(FOOTER_COLUMNS, start=1):
        slug = title.lower()
        column = site.element(f"shared/footer/{slug}", "container", parent=grid, position=column_index, parameters={"tag": "div", "class": "foot-col"})
        site.element(f"shared/footer/{slug}/title", "heading", parent=column, position=0, content=title, parameters={"level": 4})
        menu = site.element(f"shared/footer/{slug}/list", "list", parent=column, position=1)
        for position, (label, href) in enumerate(links):
            item = site.element(f"shared/footer/{slug}/item-{position}", "list_item", parent=menu, position=position)
            site.element(f"shared/footer/{slug}/link-{position}", "link", parent=item, position=0, content=label, parameters={"href": href})
    site.element("shared/footer/base", "raw_html", parent=wrap, position=2, content=read("foot-base.html"))
    return footer


MANIFESTO: list[tuple[str, str]] = [
    ("h1", "Writing began as data"),
    (
        "p",
        (
            "Around 3200 BC, in a Mesopotamian city called Uruk, someone pressed the first written signs into wet clay. "
            "They were not poems. They were records: heads of cattle, jars of beer, measures of grain — data, structured "
            "and stored so a city could function. The earliest human being whose name we know, Kushim, was not a king or "
            "a priest. He was an accountant signing grain ledgers."
        ),
    ),
    (
        "p",
        "Writing was invented as a database. Literature came two centuries later, almost as a side effect.",
    ),
    ("h2", "The web went the other way"),
    (
        "p",
        (
            "The web began as documents and grew into codebases. Today a marketing site with four pages routinely owns a "
            "repository, a dependency tree, a build pipeline, and a deploy queue. Changing a sentence means a commit, a "
            "review, a build, and a wait. The content — the thing the site exists for — is buried inside artifacts of "
            "software engineering that have nothing to say about it."
        ),
    ),
    ("h2", "A site is a record, not a build"),
    (
        "p",
        (
            "Uruk stores a website the way Uruk stored grain: as rows. Pages form a URL tree; elements form composition "
            "trees; assets and settings are rows beside them. The only committed code is a small library of generic "
            "renderers shared by every site. Editing is a write. Serving is a read. Creating a whole new site is an API "
            "call that finishes before a CI pipeline would have started."
        ),
    ),
    ("h2", "Agents are scribes"),
    (
        "p",
        (
            "Structured records want a structured editor. Uruk's first admin client is not a dashboard but an MCP server: "
            "an AI agent authenticates as a tenant user and edits the site with exactly that user's permissions — no "
            "parallel authorization model, no special agent backdoor. The scribe changed; the ledger did not."
        ),
    ),
    ("h2", "State belongs in the database"),
    (
        "p",
        (
            "One Postgres holds everything: content, users, permissions, assets, settings, even this manifesto. Backup is "
            "a database dump. Portability is a JSON archive keyed by UUIDv7 — a whole site restores anywhere, ids intact. "
            "There is no state smeared across a filesystem, an object store, and a git history."
        ),
    ),
    ("h2", "The name"),
    (
        "p",
        (
            "𒀕𒆠 — Uruk. The city where writing, and with it recorded data, began. The sign 𒆠 (KI) marks a place. "
            "Five thousand years on, this project makes a modest claim in the same spirit: every place on the web is, "
            "underneath everything, a record. Uruk just refuses to pretend otherwise."
        ),
    ),
]


def add_manifesto(site: Site, *, header: str, footer: str) -> None:
    """Add the manifesto page — deliberately built from plain heading/text elements.

    The landing pages use ``raw_html`` freely; the manifesto is composed of escaped,
    structured data blocks to show the other end of the spectrum: a page with no
    trusted HTML in it at all (bar the kicker line).
    """
    root = site.element("manifesto/root", "container", parameters={"tag": "div"})
    site.element("manifesto/header-ref", "reference", parent=root, position=0, parameters={"element_id": header})
    main = site.element("manifesto/main", "container", parent=root, position=1, parameters={"tag": "main", "class": "wrap page-shell"})
    article = site.element("manifesto/article", "container", parent=main, position=0, parameters={"tag": "article", "class": "prose prose-center"})
    site.element("manifesto/kicker", "raw_html", parent=article, position=0, content='<p class="kicker">Manifesto</p>')
    for position, (kind, text) in enumerate(MANIFESTO, start=1):
        if kind == "h1":
            site.element(f"manifesto/h-{position}", "heading", parent=article, position=position, content=str(text), parameters={"level": 1})
        elif kind == "h2":
            site.element(f"manifesto/h-{position}", "heading", parent=article, position=position, content=str(text), parameters={"level": 2})
        else:
            site.element(f"manifesto/p-{position}", "text", parent=article, position=position, content=str(text))
    site.element("manifesto/footer-ref", "reference", parent=root, position=2, parameters={"element_id": footer})
    site.page(
        "manifesto",
        "Manifesto — Uruk",
        root_element=root,
        description="Why Uruk exists: writing began as data in the first city; the web turned pages into codebases. Uruk turns them back into records.",
    )


def build() -> dict:
    """Compose the full default-site archive from the sources."""
    site = Site()

    # Assets first: the stylesheet references the font URLs, the head references all of them.
    font_names = ("martian-mono-var.woff2", "gudea-400.woff2", "gudea-700.woff2")
    font_urls = {name: site.asset(name, "font/woff2", (SRC / "fonts" / name).read_bytes()) for name in font_names}
    css = read("site.css")
    for name, url in font_urls.items():
        css = css.replace(f"{{{{asset:{name}}}}}", url)
    css_url = site.asset("site.css", "text/css", css.encode())
    js_url = site.asset("site.js", "text/javascript", read("site.js").encode())
    favicon_url = site.asset("favicon.svg", "image/svg+xml", read("favicon.svg").encode())
    site.asset("ki.svg", "image/svg+xml", read("ki.svg").encode())
    ki_asset_id = site.assets[-1]["id"]  # the header's image element resolves the logo by asset id at render time

    site.setting(
        "site.head_html",
        f'<link rel="stylesheet" href="{css_url}">'
        f'<link rel="icon" type="image/svg+xml" href="{favicon_url}">'
        f'<script defer src="{js_url}"></script>'
        f'<meta name="theme-color" content="#0a100e">',
    )

    # Shared chrome: one header and one footer element tree, referenced by every page.
    header = add_header(site, logo_asset_id=ki_asset_id)
    footer = add_footer(site)

    add_standard_page(
        site,
        slug="index",
        title="Uruk — a website is data, not code",
        description="Uruk is a multi-tenant CMS where a website is data, not code — element trees rendered, cached, and edited by agents over MCP.",
        header=header,
        footer=footer,
        sections=[
            ("hero", read("home-hero.html")),
            ("how", read("home-how.html")),
            ("features", read("home-features.html")),
            ("quickstart", read("home-quickstart.html")),
        ],
    )
    add_manifesto(site, header=header, footer=footer)
    add_standard_page(
        site,
        slug="architecture",
        title="Architecture — Uruk",
        description="How a request becomes a page: tenant routing by Host header, element-tree rendering, dependency-graph caching, portable JSON archives.",
        header=header,
        footer=footer,
        sections=[
            ("flow", read("architecture-flow.html")),
            ("model", read("architecture-model.html")),
        ],
    )
    add_standard_page(
        site,
        slug="quickstart",
        title="Quickstart — Uruk",
        description="Run Uruk locally in three commands, claim the first superuser over the API, and connect an AI agent over MCP.",
        header=header,
        footer=footer,
        sections=[("guide", read("quickstart.html"))],
    )
    add_standard_page(
        site,
        slug="configuration",
        title="Configuration — Uruk",
        description="Every URUK_* environment variable — database, cache, storage, bootstrap, JWT, MCP — and how a tenant's first superuser is claimed.",
        header=header,
        footer=footer,
        sections=[("reference", read("configuration.html"))],
    )

    # The footer brags about the site being data; give it the real numbers.
    stats = {"elements": len(site.elements), "pages": len(site.pages), "assets": len(site.assets)}
    for record in site.elements:
        for key, value in stats.items():
            record["content"] = record["content"].replace(f"{{{{stat:{key}}}}}", str(value))

    return site.archive()


def main() -> None:
    """Build the archive and write it to ``tenants/seed/default_site.json``."""
    archive = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(archive, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    size_kb = OUT.stat().st_size / 1024
    print(f"wrote {OUT} ({size_kb:.0f} kB): {len(archive['elements'])} elements, {len(archive['pages'])} pages, {len(archive['assets'])} assets")  # noqa: T201 - CLI tool


if __name__ == "__main__":
    main()
