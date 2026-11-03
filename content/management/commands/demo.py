"""``manage.py demo``: watch an agent build, publish, and draft a site over remote MCP.

Plays the agent's part with scripted tool calls, through the real HTTP endpoint
(``/mcp``) exactly as a hosted agent would: it creates a ``demo`` tenant answering on
``demo.localhost``, issues a token, builds a small blog (shared header/footer, home,
a collection of posts with RSS), publishes it, then leaves one unpublished edit for a
human to review in the Studio. Run it against a running stack::

    docker compose up -d
    docker compose exec app uv run python manage.py demo --mcp-url http://mcp:8001/mcp
"""

from __future__ import annotations

import base64
import json
import secrets
import urllib.request
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django_tenants.utils import get_public_schema_name, schema_context, schema_exists, tenant_context

from services import auth as auth_svc
from services.tenants import drop_tenant
from tenants.models import Domain, Tenant

if TYPE_CHECKING:
    from argparse import ArgumentParser

DEMO_SCHEMA = "demo"
DEMO_DOMAIN = "demo.localhost"

#: The demo site's stylesheet, uploaded as an asset by the agent.
DEMO_CSS = """
body { margin: 0; font: 17px/1.6 Georgia, serif; color: #1f2421; background: #f7f4ec; }
.wrap { max-width: 44rem; margin: 0 auto; padding: 0 1.25rem; }
header.site, footer.site { background: #1f4d43; color: #f7f4ec; }
header.site .wrap { display: flex; align-items: center; justify-content: space-between; padding: 1rem 1.25rem; }
header.site a, footer.site a { color: #f7f4ec; }
.brand { font-weight: bold; letter-spacing: .08em; text-decoration: none; }
nav ul { display: flex; gap: 1.2rem; list-style: none; margin: 0; padding: 0; }
main.wrap { padding-top: 2rem; padding-bottom: 3rem; }
h1 { font-size: 2.2rem; line-height: 1.15; margin: .4rem 0 1rem; }
ul.posts { list-style: none; padding: 0; }
ul.posts li { padding: .9rem 0; border-bottom: 1px solid #ddd6c6; }
ul.posts a { font-size: 1.15rem; color: #1f4d43; }
ul.posts time { color: #7a7466; font-size: .9rem; margin-left: .5rem; }
ul.posts p { margin: .2rem 0 0; color: #4a4f4b; }
footer.site .wrap { padding: 1.2rem 1.25rem; font-size: .9rem; }
""".strip()

POSTS = [
    ("clay-tablets", "Why clay tablets beat paper", "2026-08-02", ["history"], "Five thousand years of uptime, no backups required."),
    ("a-city-of-rows", "A city made of rows", "2026-09-14", ["uruk", "architecture"], "How a website becomes data: elements, pages, and a renderer."),
    ("drafts-and-scribes", "Scribes, drafts, and publishing", "2026-10-11", ["uruk"], "Why the agent writes drafts and a human presses publish."),
]


class Command(BaseCommand):
    """Build the demo site through the remote MCP endpoint."""

    help = "Create the 'demo' tenant (demo.localhost) and build a small blog in it over remote MCP."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept the MCP endpoint URL and a reset flag."""
        parser.add_argument("--mcp-url", default="http://localhost:8001/mcp", help="The remote MCP endpoint (inside compose: http://mcp:8001/mcp).")
        parser.add_argument("--reset", action="store_true", help="Drop an existing demo tenant first.")

    def handle(self, *args: object, **options: object) -> None:  # noqa: ARG002 - Django's fixed handle() signature
        """Provision the tenant and token, then drive the agent script over HTTP."""
        password = self._provision(reset=bool(options["reset"]))
        self.mcp_url = str(options["mcp_url"])
        self._build_site()
        self.stdout.write(self.style.SUCCESS("Demo site built and published, with one draft edit left for review."))
        self.stdout.write(f"  Site:    http://{DEMO_DOMAIN}:8000/  (RSS: /blog/feed.xml, sitemap: /sitemap.xml)")
        self.stdout.write(f"  Studio:  http://{DEMO_DOMAIN}:8000/_studio/  — sign in as demo / {password}")

    # Provisioning ------------------------------------------------------------
    def _provision(self, *, reset: bool) -> str:
        """Create the demo tenant, an editor account, and the agent's API token."""
        with schema_context(get_public_schema_name()):
            has_row = Tenant.objects.filter(schema_name=DEMO_SCHEMA).exists()
            if has_row or schema_exists(DEMO_SCHEMA):
                if not reset:
                    msg = "A demo tenant already exists; rerun with --reset to rebuild it."
                    raise CommandError(msg)
                if has_row:
                    drop_tenant(DEMO_SCHEMA)
                else:  # an orphaned schema (its Tenant row is gone): drop it too, or it would be reused
                    with connection.cursor() as cursor:
                        cursor.execute(f'DROP SCHEMA "{DEMO_SCHEMA}" CASCADE')
            tenant = Tenant.objects.create(schema_name=DEMO_SCHEMA, name="Uruk demo")
            Domain.objects.create(domain=DEMO_DOMAIN, tenant=tenant, is_primary=True)
        password = secrets.token_urlsafe(9)
        with tenant_context(tenant):
            human = User.objects.create_superuser("demo", "demo@example.com", password)
            _, self.token = auth_svc.create_token(human, name="demo agent")
        return password

    # The "agent" -------------------------------------------------------------
    def _build_site(self) -> None:
        """Make the tool calls an agent would make to build, publish, and then draft."""
        css = self._call("upload_asset", name="demo.css", content_type="text/css", data_base64=_b64(DEMO_CSS))
        self._call("set_setting", key="site.head_html", value=f'<link rel="stylesheet" href="{css["url"]}">')

        header = self._element("container", parameters={"tag": "header", "class": "site"}, is_reusable=True)
        bar = self._element("container", parent_id=header, parameters={"class": "wrap"})
        self._element("link", parent_id=bar, content="URUK FIELD NOTES", parameters={"href": "/", "class": "brand"})
        nav = self._element("nav", parent_id=bar, position=1)
        links = self._element("list", parent_id=nav)
        for position, (label, href) in enumerate([("Home", "/"), ("Blog", "/blog"), ("RSS", "/blog/feed.xml")]):
            item = self._element("list_item", parent_id=links, position=position)
            self._element("link", parent_id=item, content=label, parameters={"href": href})
        footer = self._element("container", parameters={"tag": "footer", "class": "site"}, is_reusable=True)
        foot = self._element("container", parent_id=footer, parameters={"class": "wrap"})
        self._element("text", parent_id=foot, content="Built by an agent over MCP, reviewed by a human, served from rows.")

        blog_root = self._page_root(header, footer)
        blog = self._call("create_page", slug="blog", name="Blog", root_element_id=blog_root["page"])
        self._call("update_page", page_id=blog["id"], parameters={"summary": "Notes from a city of rows"})
        self._element("heading", parent_id=blog_root["main"], content="All posts")
        self._element("page_list", parent_id=blog_root["main"], position=1, parameters={"parent_id": blog["id"], "class": "posts"})

        for slug, title, date, tags, summary in POSTS:
            root = self._page_root(header, footer)
            self._element("heading", parent_id=root["main"], content=title)
            self._element("text", parent_id=root["main"], position=1, content=summary)
            self._element("link", parent_id=root["main"], position=2, content="← All posts", parameters={"href": "/blog"})
            page = self._call("create_page", slug=slug, name=title, root_element_id=root["page"], parent_id=blog["id"])
            self._call("update_page", page_id=page["id"], parameters={"date": date, "tags": tags, "summary": summary})

        home = self._page_root(header, footer)
        self._element("heading", parent_id=home["main"], content="Field notes from a city of rows")
        intro = self._element("text", parent_id=home["main"], position=1, content="A small blog an agent built in a few seconds.")
        self._element("heading", parent_id=home["main"], position=2, content="Latest", parameters={"level": 2})
        self._element("page_list", parent_id=home["main"], position=3, parameters={"parent_id": blog["id"], "limit": 2, "class": "posts"})
        self._call("create_page", slug="index", name="Uruk field notes", root_element_id=home["page"])

        self._call("publish", message="Launch the demo blog")
        # A last edit stays a draft: visitors don't see it until someone publishes.
        self._call("update_element", element_id=intro, content="A small blog an agent built in a few seconds — and a human reviewed before it went live.")

    def _page_root(self, header: str, footer: str) -> dict[str, str]:
        """Create a page skeleton: shared header, a ``<main>`` to fill, shared footer."""
        page = self._element("container")
        self._element("reference", parent_id=page, parameters={"element_id": header})
        main = self._element("container", parent_id=page, position=1, parameters={"tag": "main", "class": "wrap"})
        self._element("reference", parent_id=page, position=2, parameters={"element_id": footer})
        return {"page": page, "main": main}

    def _element(self, kind: str, **arguments: object) -> str:
        """Create an element and return its id."""
        return self._call("create_element", kind=kind, **arguments)["id"]

    def _call(self, tool: str, **arguments: object) -> dict:
        """Call one MCP tool over HTTP, as a remote agent, and return its result."""
        response = post_json(
            self.mcp_url,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
            {"Host": DEMO_DOMAIN, "Authorization": f"Bearer {self.token}", "Accept": "application/json, text/event-stream"},
        )
        result = response.get("result") or {}
        if "error" in response or result.get("isError"):
            msg = f"{tool} failed: {response.get('error') or result.get('content')}"
            raise CommandError(msg)
        structured = result.get("structuredContent")
        if structured is not None:
            # Object-typed tools return the object itself; lists and scalars come wrapped as {"result": ...}.
            return structured["result"] if set(structured) == {"result"} else structured
        return json.loads(result["content"][0]["text"])


def post_json(url: str, payload: dict, headers: dict[str, str]) -> dict:
    """POST a JSON body and decode the JSON reply (module-level so tests can swap the transport)."""
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", **headers}, method="POST")  # noqa: S310 - the URL is the operator's own MCP endpoint
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - see above
        return json.loads(response.read())


def _b64(text: str) -> str:
    """Base64-encode a text asset for ``upload_asset``."""
    return base64.b64encode(text.encode()).decode()
