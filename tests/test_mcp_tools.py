"""MCP tools build and render a site as a permission-scoped agent (§11)."""

import base64

import pytest
from django.contrib.auth.models import Permission, User
from django_tenants.utils import tenant_context

from mcp_server import tools
from mcp_server.context import AgentContext
from services import auth as auth_svc
from tenants.models import Tenant


def _agent_context(schema: str, *, with_perms: bool) -> AgentContext:
    tenant = Tenant.objects.create(schema_name=schema, name=schema)
    with tenant_context(tenant):
        user = User.objects.create_user("agent")
        if with_perms:
            user.user_permissions.set(Permission.objects.filter(content_type__app_label="content"))
        _, token = auth_svc.create_token(user)
    return AgentContext(tenant=tenant, token=token)


@pytest.mark.django_db(transaction=True)
def test_agent_builds_and_renders_page() -> None:
    ctx = _agent_context("mcp_build", with_perms=True)

    root = tools.create_element(ctx, "container", parameters={"tag": "section"})
    tools.create_element(ctx, "text", parent_id=root["id"], content="Hi")
    page = tools.create_page(ctx, "index", "Index", root_element_id=root["id"])

    # The agent can see the rendered result of its edits.
    assert tools.render_page(ctx, page["id"]) == "<section><p>Hi</p></section>"
    assert [p["slug"] for p in tools.list_pages(ctx)] == ["index"]


@pytest.mark.django_db(transaction=True)
def test_agent_edit_and_move() -> None:
    ctx = _agent_context("mcp_edit", with_perms=True)

    root = tools.create_element(ctx, "container")
    a = tools.create_element(ctx, "text", parent_id=root["id"], position=0, content="a")
    b = tools.create_element(ctx, "text", parent_id=root["id"], position=1, content="b")

    updated = tools.update_element(ctx, a["id"], content="A")
    assert updated["content"] == "A"

    moved = tools.move_element(ctx, b["id"], parent_id=root["id"], position=0)
    assert moved["position"] == 0

    tools.delete_element(ctx, a["id"])
    page = tools.create_page(ctx, "index", "Index", root_element_id=root["id"])
    assert tools.render_page(ctx, page["id"]) == "<div><p>b</p></div>"


@pytest.mark.django_db(transaction=True)
def test_agent_discovers_existing_structure_and_edits_it() -> None:
    ctx = _agent_context("mcp_read", with_perms=True)

    root = tools.create_element(ctx, "container", parameters={"tag": "section"})
    child = tools.create_element(ctx, "text", parent_id=root["id"], content="old")
    home = tools.create_page(ctx, "index", "Home", root_element_id=root["id"])
    tools.create_page(ctx, "about", "About", parent_id=home["id"])

    # The agent navigates pages as a tree, not a flat list.
    pages = tools.get_page_tree(ctx)
    assert [page["slug"] for page in pages] == ["index"]
    subpages = pages[0]["children"]
    assert isinstance(subpages, list)
    assert [page["slug"] for page in subpages] == ["about"]

    # From the page it discovers the element tree — ids included — and edits by id.
    root_element_id = pages[0]["root_element_id"]
    assert isinstance(root_element_id, str)
    tree = tools.get_element_tree(ctx, root_element_id)
    assert tree["kind"] == "container"
    nodes = tree["children"]
    assert isinstance(nodes, list)
    assert [node["id"] for node in nodes] == [child["id"]]

    tools.update_element(ctx, nodes[0]["id"], content="new")
    assert tools.render_page(ctx, home["id"]) == "<section><p>new</p></section>"

    assert tools.get_element(ctx, child["id"])["content"] == "new"
    assert {element["id"] for element in tools.list_elements(ctx)} == {root["id"], child["id"]}

    # The agent can discover the renderer vocabulary instead of guessing kind names.
    assert {"container", "image", "link", "list", "list_item", "nav"} <= set(tools.list_element_kinds(ctx))


@pytest.mark.django_db(transaction=True)
def test_agent_page_lifecycle() -> None:
    ctx = _agent_context("mcp_pages", with_perms=True)

    page = tools.create_page(ctx, "index", "Index")
    root = tools.create_element(ctx, "text", content="hi")

    page = tools.set_page_root_element(ctx, page["id"], root_element_id=root["id"])
    assert page["root_element_id"] == root["id"]
    assert tools.render_page(ctx, page["id"]) == "<p>hi</p>"

    page = tools.update_page(ctx, page["id"], name="Home")
    assert page["name"] == "Home"

    tools.delete_page(ctx, page["id"])
    assert tools.list_pages(ctx) == []


@pytest.mark.django_db(transaction=True)
def test_agent_reorders_children() -> None:
    ctx = _agent_context("mcp_reorder", with_perms=True)

    root = tools.create_element(ctx, "container")
    a = tools.create_element(ctx, "text", parent_id=root["id"], position=0, content="a")
    b = tools.create_element(ctx, "text", parent_id=root["id"], position=1, content="b")

    reordered = tools.reorder_children(ctx, root["id"], [b["id"], a["id"]])
    assert [child["content"] for child in reordered] == ["b", "a"]

    page = tools.create_page(ctx, "index", "Index", root_element_id=root["id"])
    assert tools.render_page(ctx, page["id"]) == "<div><p>b</p><p>a</p></div>"


@pytest.mark.django_db(transaction=True)
def test_agent_uploads_and_lists_assets() -> None:
    ctx = _agent_context("mcp_assets", with_perms=True)

    uploaded = tools.upload_asset(ctx, "main.css", "text/css", base64.b64encode(b"body{}").decode())
    assets = tools.list_assets(ctx)
    assert [asset["name"] for asset in assets] == ["main.css"]
    assert assets[0]["url"] == f"/_assets/{uploaded['sha256']}/main.css"


@pytest.mark.django_db(transaction=True)
def test_tool_enforces_permissions() -> None:
    ctx = _agent_context("mcp_perm", with_perms=False)

    with pytest.raises(tools.PermissionDeniedError):
        tools.create_element(ctx, "text", content="nope")

    with pytest.raises(tools.PermissionDeniedError):
        tools.get_page_tree(ctx)


@pytest.mark.django_db(transaction=True)
def test_agent_can_read_a_kinds_documentation() -> None:
    ctx = _agent_context("mcp_kind_doc", with_perms=True)

    assert "page_list" in tools.list_element_kinds(ctx)
    assert "parent_id" in tools.describe_element_kind(ctx, "page_list")
    with pytest.raises(ValueError, match="Unknown element kind"):
        tools.describe_element_kind(ctx, "nope")
