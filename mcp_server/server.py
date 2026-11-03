"""Uruk MCP server: registers the service-layer tools for an agent.

Two transports share these tools. **stdio** — ``python -m mcp_server`` with
``URUK_MCP_TENANT`` and ``URUK_MCP_TOKEN`` set — for a locally launched agent. **HTTP**
— :mod:`mcp_server.http`, served at ``/mcp`` — for hosted agents connecting remotely,
where every request authenticates with its own bearer token (ARCHITECTURE.md §11).
Sync tools, because django-tenants is sync-bound (see ARCHITECTURE.md).
"""

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from functools import lru_cache  # noqa: E402  (Django must be configured before model imports)
from typing import TYPE_CHECKING  # noqa: E402

from django.db import connection  # noqa: E402
from mcp.server.mcpserver import Context, MCPServer  # noqa: E402

from mcp_server import tools  # noqa: E402
from mcp_server.context import AgentContext, build_context_from_env, build_context_from_headers  # noqa: E402
from mcp_server.tools import AssetData, ElementData, PageData  # noqa: E402, TC001 - the MCP SDK reads return annotations at runtime for output schemas
from services.publishing import PublishingStatus, RevisionSummary  # noqa: E402, TC001 - read at runtime, as below

if TYPE_CHECKING:
    from collections.abc import Callable

mcp = MCPServer("Uruk")


@lru_cache
def _env_context() -> AgentContext:
    """Build the stdio connection's fixed tenant + token from ``URUK_MCP_TENANT`` / ``URUK_MCP_TOKEN``."""
    return build_context_from_env()


def _run[T](ctx: Context, tool: Callable[..., T], *args: object, **kwargs: object) -> T:
    """Run a tool as the calling agent: per-request (HTTP) or per-process (stdio) identity.

    Over HTTP each call carries its own ``Authorization`` bearer token and ``Host`` (which
    selects the tenant, exactly as for the website); tools then run in a worker thread,
    whose DB connection is handed back afterwards. Over stdio the environment decides.
    """
    headers = ctx.headers
    if headers is None:
        return tool(_env_context(), *args, **kwargs)
    try:
        return tool(build_context_from_headers(headers), *args, **kwargs)
    finally:
        connection.close()


@mcp.tool()
def list_pages(ctx: Context) -> list[PageData]:
    """List all pages in the current site."""
    return _run(ctx, tools.list_pages)


@mcp.tool()
def get_page_tree(ctx: Context) -> list[dict]:
    """Return the page hierarchy as a nested tree (descendants under "children")."""
    return _run(ctx, tools.get_page_tree)


@mcp.tool()
def create_page(ctx: Context, slug: str, name: str, root_element_id: str | None = None, parent_id: str | None = None) -> PageData:
    """Create a page, optionally rooted at an element and nested under a parent page."""
    return _run(ctx, tools.create_page, slug, name, root_element_id=root_element_id, parent_id=parent_id)


@mcp.tool()
def update_page(ctx: Context, page_id: str, slug: str | None = None, name: str | None = None, parameters: dict | None = None) -> PageData:
    """Update a page's slug, name, and/or parameters (omitted fields are left unchanged)."""
    return _run(ctx, tools.update_page, page_id, slug=slug, name=name, parameters=parameters)


@mcp.tool()
def set_page_root_element(ctx: Context, page_id: str, root_element_id: str | None) -> PageData:
    """Set (or clear, with null) the element tree a page renders from."""
    return _run(ctx, tools.set_page_root_element, page_id, root_element_id=root_element_id)


@mcp.tool()
def delete_page(ctx: Context, page_id: str) -> dict:
    """Delete a page (its elements are kept — they may be shared)."""
    return _run(ctx, tools.delete_page, page_id)


@mcp.tool()
def list_element_kinds(ctx: Context) -> list[str]:
    """List the element kinds the renderer understands — the vocabulary for create_element."""
    return _run(ctx, tools.list_element_kinds)


@mcp.tool()
def describe_element_kind(ctx: Context, kind: str) -> str:
    """Explain one element kind: what it renders and which parameters/content it reads."""
    return _run(ctx, tools.describe_element_kind, kind)


@mcp.tool()
def list_elements(ctx: Context) -> list[ElementData]:
    """List all elements as a flat list (parent_id/position carry the structure)."""
    return _run(ctx, tools.list_elements)


@mcp.tool()
def get_element(ctx: Context, element_id: str) -> ElementData:
    """Fetch a single element."""
    return _run(ctx, tools.get_element, element_id)


@mcp.tool()
def get_element_tree(ctx: Context, element_id: str) -> dict:
    """Return an element's subtree as a nested tree — read existing structure (with ids) before editing it."""
    return _run(ctx, tools.get_element_tree, element_id)


@mcp.tool()
def create_element(
    ctx: Context,
    kind: str,
    parent_id: str | None = None,
    position: int = 0,
    content: str = "",
    parameters: dict | None = None,
    is_reusable: bool = False,  # noqa: FBT001, FBT002 - MCP passes tool args by name, not positionally
) -> ElementData:
    """Create an element of the given kind, optionally under a parent at a position.

    Mark shared trees (a header/footer included via ``reference``) with ``is_reusable``.
    """
    return _run(ctx, tools.create_element, kind, parent_id=parent_id, position=position, content=content, parameters=parameters, is_reusable=is_reusable)


@mcp.tool()
def update_element(
    ctx: Context,
    element_id: str,
    kind: str | None = None,
    content: str | None = None,
    parameters: dict | None = None,
    is_reusable: bool | None = None,  # noqa: FBT001 - MCP passes tool args by name, not positionally
) -> ElementData:
    """Update an element's fields (omitted fields are left unchanged)."""
    return _run(ctx, tools.update_element, element_id, kind=kind, content=content, parameters=parameters, is_reusable=is_reusable)


@mcp.tool()
def move_element(ctx: Context, element_id: str, parent_id: str | None, position: int) -> ElementData:
    """Move an element to a new parent and position."""
    return _run(ctx, tools.move_element, element_id, parent_id=parent_id, position=position)


@mcp.tool()
def reorder_children(ctx: Context, parent_id: str, ordered_ids: list[str]) -> list[ElementData]:
    """Reorder a parent's direct children to match the given id order."""
    return _run(ctx, tools.reorder_children, parent_id, ordered_ids)


@mcp.tool()
def delete_element(ctx: Context, element_id: str) -> dict:
    """Delete an element and its subtree."""
    return _run(ctx, tools.delete_element, element_id)


@mcp.tool()
def set_setting(ctx: Context, key: str, value: object) -> dict:
    """Create or update a configuration setting."""
    return _run(ctx, tools.set_setting, key, value)


@mcp.tool()
def get_settings(ctx: Context) -> dict:
    """Return all settings as a key -> value mapping."""
    return _run(ctx, tools.get_settings)


@mcp.tool()
def list_assets(ctx: Context) -> list[AssetData]:
    """List all assets (metadata and serving URL, not the bytes)."""
    return _run(ctx, tools.list_assets)


@mcp.tool()
def upload_asset(ctx: Context, name: str, content_type: str, data_base64: str) -> AssetData:
    """Upload an asset from base64-encoded bytes; returns metadata including its immutable URL."""
    return _run(ctx, tools.upload_asset, name, content_type, data_base64)


@mcp.tool()
def render_page(ctx: Context, page_id: str) -> str:
    """Render a page's working copy to HTML so the agent can see its (unpublished) edits."""
    return _run(ctx, tools.render_page, page_id)


@mcp.tool()
def get_publishing_status(ctx: Context) -> PublishingStatus:
    """Show whether the site is in live or draft mode, which revision is live, and what is unpublished.

    Once a site has been published, edits only reach the working copy; visitors see the
    live revision until you call publish.
    """
    return _run(ctx, tools.get_publishing_status)


@mcp.tool()
def publish(ctx: Context, message: str = "") -> RevisionSummary:
    """Publish the working copy: visitors see exactly what render_page shows, from now on."""
    return _run(ctx, tools.publish, message)


@mcp.tool()
def list_revisions(ctx: Context) -> list[RevisionSummary]:
    """List the publish history, newest first."""
    return _run(ctx, tools.list_revisions)


@mcp.tool()
def rollback(ctx: Context, revision_id: str) -> RevisionSummary:
    """Make an older revision live again (as a new revision); the working copy is not changed."""
    return _run(ctx, tools.rollback, revision_id)


@mcp.tool()
def discard_draft(ctx: Context, revision_id: str | None = None) -> PublishingStatus:
    """Throw away unpublished changes, resetting the working copy to a revision (default: the live one)."""
    return _run(ctx, tools.discard_draft, revision_id)


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
