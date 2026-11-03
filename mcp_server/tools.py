"""Tool implementations: plain functions over the service layer (testable directly).

Each runs inside the connection's tenant schema and enforces the acting user's
Django permissions before calling ``services.content``. Returns are JSON-safe
(UUIDs as strings) for transport over MCP.
"""

from __future__ import annotations

import base64
import inspect
from contextlib import contextmanager
from typing import TYPE_CHECKING, TypedDict
from uuid import UUID

from django_tenants.utils import tenant_context

from caching.service import render_page_cached
from content.models import Asset, Element, Page, Revision
from content.views import asset_url
from rendering import RENDERERS
from services import auth as auth_svc
from services import content as svc
from services import publishing as publishing_svc

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Mapping

    from django.contrib.auth.models import User

    from mcp_server.context import AgentContext
    from services.publishing import PublishingStatus, RevisionSummary


class AuthenticationError(Exception):
    """Raised when the connection's token does not resolve to a tenant user."""


class PermissionDeniedError(Exception):
    """Raised when the acting user lacks the Django permission for a tool."""


@contextmanager
def _authorized(ctx: AgentContext, perm: str) -> Generator[User]:
    """Enter the tenant schema, authenticate the token, require ``perm``; yield the acting user."""
    with tenant_context(ctx.tenant):
        user = auth_svc.resolve_token(ctx.token)
        if user is None:
            msg = "Invalid or expired API token"
            raise AuthenticationError(msg)
        if not user.has_perm(perm):
            msg = f"Missing permission: {perm}"
            raise PermissionDeniedError(msg)
        yield user


class ElementData(TypedDict):
    """An element as the tools return it."""

    id: str
    kind: str
    parent_id: str | None
    position: int
    content: str
    parameters: dict[str, object]
    is_reusable: bool


class PageData(TypedDict):
    """A page as the tools return it."""

    id: str
    slug: str
    name: str
    parent_id: str | None
    root_element_id: str | None
    position: int
    parameters: dict[str, object]


class AssetData(TypedDict):
    """An asset's metadata as the tools return it (never its bytes)."""

    id: str
    name: str
    content_type: str
    sha256: str
    url: str


def _element_dict(element: Element) -> ElementData:
    return {
        "id": str(element.pk),
        "kind": element.kind,
        "parent_id": str(element.parent_id) if element.parent_id else None,
        "position": element.position,
        "content": element.content,
        "parameters": element.parameters,
        "is_reusable": element.is_reusable,
    }


def _page_dict(page: Page) -> PageData:
    return {
        "id": str(page.pk),
        "slug": page.slug,
        "name": page.name,
        "parent_id": str(page.parent_id) if page.parent_id else None,
        "root_element_id": str(page.root_element_id) if page.root_element_id else None,
        "position": page.position,
        "parameters": page.parameters,
    }


def _asset_dict(asset: Asset) -> AssetData:
    return {
        "id": str(asset.pk),
        "name": asset.name,
        "content_type": asset.content_type,
        "sha256": asset.sha256,
        "url": asset_url(asset),
    }


# A "tree row" is anything that self-nests via parent/position — Element and Page both do.
def _children_map[TreeRow: (Element, Page)](rows: Iterable[TreeRow]) -> dict[UUID | None, list[TreeRow]]:
    """Group rows by ``parent_id``, preserving the queryset's position ordering."""
    by_parent: dict[UUID | None, list[TreeRow]] = {}
    for row in rows:
        by_parent.setdefault(row.parent_id, []).append(row)
    return by_parent


def _nested[TreeRow: (Element, Page)](
    row: TreeRow,
    by_parent: dict[UUID | None, list[TreeRow]],
    serialize: Callable[[TreeRow], Mapping[str, object]],
) -> dict[str, object]:
    """Serialize a row with its descendants nested under a ``children`` key."""
    return {**serialize(row), "children": [_nested(child, by_parent, serialize) for child in by_parent.get(row.pk, [])]}


def list_pages(ctx: AgentContext) -> list[PageData]:
    """Return all pages."""
    with _authorized(ctx, "content.view_page"):
        return [_page_dict(page) for page in svc.list_pages()]


def get_page_tree(ctx: AgentContext) -> list[dict[str, object]]:
    """Return the page hierarchy as a nested tree (top-level pages, descendants under ``children``)."""
    with _authorized(ctx, "content.view_page"):
        by_parent = _children_map(svc.list_pages())
        return [_nested(page, by_parent, _page_dict) for page in by_parent.get(None, [])]


def create_page(ctx: AgentContext, slug: str, name: str, *, root_element_id: str | None = None, parent_id: str | None = None) -> PageData:
    """Create a page, optionally rooted at an element and nested under a parent page."""
    with _authorized(ctx, "content.add_page"):
        root = Element.objects.get(pk=root_element_id) if root_element_id else None
        parent = Page.objects.get(pk=parent_id) if parent_id else None
        return _page_dict(svc.create_page(slug, name, parent=parent, root_element=root))


def update_page(ctx: AgentContext, page_id: str, *, slug: str | None = None, name: str | None = None, parameters: dict | None = None) -> PageData:
    """Update a page's slug, name, and/or parameters (omitted fields are left unchanged)."""
    with _authorized(ctx, "content.change_page"):
        page = Page.objects.get(pk=page_id)
        return _page_dict(svc.update_page(page, slug=slug, name=name, parameters=parameters))


def set_page_root_element(ctx: AgentContext, page_id: str, *, root_element_id: str | None) -> PageData:
    """Set (or clear, with ``None``) the element tree a page renders from."""
    with _authorized(ctx, "content.change_page"):
        page = Page.objects.get(pk=page_id)
        root = Element.objects.get(pk=root_element_id) if root_element_id else None
        return _page_dict(svc.set_page_root_element(page, root))


def delete_page(ctx: AgentContext, page_id: str) -> dict[str, object]:
    """Delete a page (its elements are kept — they may be shared)."""
    with _authorized(ctx, "content.delete_page"):
        svc.delete_page(Page.objects.get(pk=page_id))
        return {"deleted": page_id}


def list_element_kinds(ctx: AgentContext) -> list[str]:
    """Return the element kinds the renderer understands — the vocabulary for ``create_element``."""
    with _authorized(ctx, "content.view_element"):
        return sorted(RENDERERS)


def describe_element_kind(ctx: AgentContext, kind: str) -> str:
    """Return a kind's documentation (its renderer's docstring): what it renders and its parameters."""
    with _authorized(ctx, "content.view_element"):
        renderer = RENDERERS.get(kind)
        if renderer is None:
            msg = f"Unknown element kind {kind!r}; see list_element_kinds"
            raise ValueError(msg)
        return inspect.getdoc(renderer) or ""


def list_elements(ctx: AgentContext) -> list[ElementData]:
    """Return all elements as a flat list (``parent_id``/``position`` carry the structure)."""
    with _authorized(ctx, "content.view_element"):
        return [_element_dict(element) for element in svc.list_elements()]


def get_element(ctx: AgentContext, element_id: str) -> ElementData:
    """Fetch a single element."""
    with _authorized(ctx, "content.view_element"):
        return _element_dict(Element.objects.get(pk=element_id))


def get_element_tree(ctx: AgentContext, element_id: str) -> dict[str, object]:
    """Return an element's subtree as a nested tree — how the agent reads existing structure (with ids) before editing it.

    Fetches all elements in one query and assembles the subtree in memory, so the
    cost is one round-trip regardless of depth. ``reference`` elements are *not*
    followed — the referenced tree is separate data; read it with another call.
    """
    with _authorized(ctx, "content.view_element"):
        root = Element.objects.get(pk=element_id)
        return _nested(root, _children_map(svc.list_elements()), _element_dict)


def create_element(
    ctx: AgentContext,
    kind: str,
    *,
    parent_id: str | None = None,
    position: int = 0,
    content: str = "",
    parameters: dict | None = None,
    is_reusable: bool = False,
) -> ElementData:
    """Create an element, optionally under a parent at a position (``is_reusable`` marks shared trees)."""
    with _authorized(ctx, "content.add_element"):
        parent = Element.objects.get(pk=parent_id) if parent_id else None
        element = svc.create_element(kind, parent=parent, position=position, content=content, parameters=parameters, is_reusable=is_reusable)
        return _element_dict(element)


def update_element(
    ctx: AgentContext,
    element_id: str,
    *,
    kind: str | None = None,
    content: str | None = None,
    parameters: dict | None = None,
    is_reusable: bool | None = None,
) -> ElementData:
    """Update an element's fields (omitted fields are left unchanged)."""
    with _authorized(ctx, "content.change_element"):
        element = Element.objects.get(pk=element_id)
        element = svc.update_element(element, kind=kind, content=content, parameters=parameters, is_reusable=is_reusable)
        return _element_dict(element)


def move_element(ctx: AgentContext, element_id: str, *, parent_id: str | None, position: int) -> ElementData:
    """Move an element to a new parent and position."""
    with _authorized(ctx, "content.change_element"):
        element = Element.objects.get(pk=element_id)
        parent = Element.objects.get(pk=parent_id) if parent_id else None
        return _element_dict(svc.move_element(element, parent=parent, position=position))


def reorder_children(ctx: AgentContext, parent_id: str, ordered_ids: list[str]) -> list[ElementData]:
    """Reorder a parent's direct children to match ``ordered_ids``; returns them in the new order."""
    with _authorized(ctx, "content.change_element"):
        parent = Element.objects.get(pk=parent_id)
        svc.reorder_children(parent, [UUID(element_id) for element_id in ordered_ids])
        return [_element_dict(child) for child in parent.children.all()]


def delete_element(ctx: AgentContext, element_id: str) -> dict[str, object]:
    """Delete an element and its subtree."""
    with _authorized(ctx, "content.delete_element"):
        svc.delete_element(Element.objects.get(pk=element_id))
        return {"deleted": element_id}


def set_setting(ctx: AgentContext, key: str, value: object) -> dict[str, object]:
    """Create or update a setting."""
    with _authorized(ctx, "content.change_setting"):
        setting = svc.set_setting(key, value)
        return {"key": setting.key, "value": setting.value}


def get_settings(ctx: AgentContext) -> dict[str, object]:
    """Return all settings as a key -> value mapping."""
    with _authorized(ctx, "content.view_setting"):
        return svc.get_settings()


def list_assets(ctx: AgentContext) -> list[AssetData]:
    """Return all assets (metadata and serving URL, not the bytes)."""
    with _authorized(ctx, "content.view_asset"):
        return [_asset_dict(asset) for asset in svc.list_assets()]


def upload_asset(ctx: AgentContext, name: str, content_type: str, data_base64: str) -> AssetData:
    """Upload an asset (base64 bytes); returns its metadata including the immutable URL."""
    with _authorized(ctx, "content.add_asset"):
        return _asset_dict(svc.upload_asset(name, content_type, base64.b64decode(data_base64)))


def render_page(ctx: AgentContext, page_id: str) -> str:
    """Render a page's working copy to HTML (cached) — lets the agent see its unpublished edits."""
    with _authorized(ctx, "content.view_page"):
        return str(render_page_cached(Page.objects.get(pk=page_id)))


def get_publishing_status(ctx: AgentContext) -> PublishingStatus:
    """Return the mode (live/draft), the live revision, and the unpublished changes."""
    with _authorized(ctx, "content.view_revision"):
        return publishing_svc.status()


def publish(ctx: AgentContext, message: str = "") -> RevisionSummary:
    """Publish the working copy as the new live revision."""
    with _authorized(ctx, "content.add_revision") as user:
        return publishing_svc.revision_summary(publishing_svc.publish(message=message, author=user))


def list_revisions(ctx: AgentContext) -> list[RevisionSummary]:
    """Return the publish history, newest first."""
    with _authorized(ctx, "content.view_revision"):
        return [publishing_svc.revision_summary(revision) for revision in publishing_svc.list_revisions()]


def rollback(ctx: AgentContext, revision_id: str) -> RevisionSummary:
    """Make an older revision live again, as a new revision (the working copy is untouched)."""
    with _authorized(ctx, "content.add_revision") as user:
        return publishing_svc.revision_summary(publishing_svc.rollback(Revision.objects.get(pk=revision_id), author=user))


def discard_draft(ctx: AgentContext, revision_id: str | None = None) -> PublishingStatus:
    """Reset the working copy to a revision (default: the live one), dropping unpublished changes."""
    with _authorized(ctx, "content.discard_draft"):
        publishing_svc.discard_draft(Revision.objects.get(pk=revision_id) if revision_id else None)
        return publishing_svc.status()
