"""MCP over HTTP: a remote agent drives a tenant through ``/mcp`` with its bearer token (§11)."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

import pytest
from django.contrib.auth.models import Permission, User
from django_tenants.utils import tenant_context
from starlette.testclient import TestClient

from mcp_server.http import create_app
from services import auth as auth_svc
from tenants.models import Domain, Tenant

if TYPE_CHECKING:
    from collections.abc import Iterator

ACCEPT = {"accept": "application/json, text/event-stream"}


def _tenant_with_token(schema: str, domain: str) -> str:
    """Create a routed tenant and an all-content-permissions agent token for it."""
    tenant = Tenant.objects.create(schema_name=schema, name=schema)
    Domain.objects.create(domain=domain, tenant=tenant, is_primary=True)
    with tenant_context(tenant):
        agent = User.objects.create_user("agent")
        agent.user_permissions.set(Permission.objects.filter(content_type__app_label="content"))
        _, token = auth_svc.create_token(agent)
    return token


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Yield an HTTP client over a fresh MCP ASGI app, with its lifespan running."""
    with TestClient(create_app()) as test_client:
        yield test_client


def _rpc(client: TestClient, host: str, token: str, method: str, params: dict | None = None) -> dict:
    """POST one JSON-RPC request to /mcp and return its decoded response."""
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={**ACCEPT, "host": host, "authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _call(client: TestClient, host: str, token: str, tool: str, **arguments: object) -> Any:  # noqa: ANN401 - tool results are untyped JSON
    """Call a tool and return its structured result."""
    result = _rpc(client, host, token, "tools/call", {"name": tool, "arguments": arguments})["result"]
    assert not result.get("isError"), result
    structured = result.get("structuredContent")
    if structured is not None:
        # Object-typed tools return the object itself; lists and scalars come wrapped as {"result": ...}.
        return structured["result"] if set(structured) == {"result"} else structured
    return json.loads(result["content"][0]["text"])


@pytest.mark.django_db(transaction=True)
def test_requests_without_a_valid_token_are_rejected(client: TestClient) -> None:
    _tenant_with_token("mcph_auth", "mcph-auth.localhost")
    host = {"host": "mcph-auth.localhost"}

    missing = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={**ACCEPT, **host})
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"].startswith("Bearer")

    bogus = client.post("/mcp", json={}, headers={**ACCEPT, **host, "authorization": "Bearer uruk_nope"})
    assert bogus.status_code == 401


@pytest.mark.django_db(transaction=True)
def test_remote_agent_builds_and_publishes_a_page(client: TestClient) -> None:
    token = _tenant_with_token("mcph_build", "mcph-build.localhost")
    host = "mcph-build.localhost"

    names = {tool["name"] for tool in _rpc(client, host, token, "tools/list")["result"]["tools"]}
    assert {"create_element", "render_page", "publish"} <= names

    root = _call(client, host, token, "create_element", kind="container", parameters={"tag": "main"})
    _call(client, host, token, "create_element", kind="text", parent_id=root["id"], content="Hello over HTTP")
    page = _call(client, host, token, "create_page", slug="index", name="Home", root_element_id=root["id"])
    assert _call(client, host, token, "render_page", page_id=page["id"]) == "<main><p>Hello over HTTP</p></main>"
    assert _call(client, host, token, "publish", message="From a remote agent")["number"] == 1


@pytest.mark.django_db(transaction=True)
def test_a_token_only_works_on_its_own_tenant(client: TestClient) -> None:
    token_a = _tenant_with_token("mcph_iso_a", "mcph-iso-a.localhost")
    _tenant_with_token("mcph_iso_b", "mcph-iso-b.localhost")

    # Tenant A's token, aimed at tenant B's host: not a user there.
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={**ACCEPT, "host": "mcph-iso-b.localhost", "authorization": f"Bearer {token_a}"},
    )
    assert response.status_code == 401


@pytest.mark.django_db(transaction=True)
def test_every_tool_is_reachable_over_http(client: TestClient) -> None:
    token = _tenant_with_token("mcph_all", "mcph-all.localhost")

    def call(tool: str, **arguments: object) -> Any:  # noqa: ANN401 - tool results are untyped JSON
        return _call(client, "mcph-all.localhost", token, tool, **arguments)

    # Element kinds and element CRUD.
    assert "text" in call("list_element_kinds")
    assert "text" in call("describe_element_kind", kind="text")
    root = call("create_element", kind="container", parameters={"tag": "main"})
    first = call("create_element", kind="text", parent_id=root["id"], content="First")
    second = call("create_element", kind="text", parent_id=root["id"], position=1, content="Second")
    assert call("update_element", element_id=first["id"], content="One")["content"] == "One"
    assert [row["id"] for row in call("reorder_children", parent_id=root["id"], ordered_ids=[second["id"], first["id"]])] == [second["id"], first["id"]]
    assert call("get_element", element_id=root["id"])["kind"] == "container"
    assert [child["content"] for child in call("get_element_tree", element_id=root["id"])["children"]] == ["Second", "One"]
    assert len(call("list_elements")) == 3

    # Pages.
    page = call("create_page", slug="index", name="Home", root_element_id=root["id"])
    assert call("update_page", page_id=page["id"], name="Start")["name"] == "Start"
    assert call("list_pages")[0]["name"] == "Start"
    assert call("get_page_tree")[0]["slug"] == "index"
    assert call("render_page", page_id=page["id"]) == "<main><p>Second</p><p>One</p></main>"

    # Settings and assets.
    assert call("set_setting", key="site.title", value="All tools") == {"key": "site.title", "value": "All tools"}
    assert call("get_settings")["site.title"] == "All tools"
    asset = call("upload_asset", name="a.txt", content_type="text/plain", data_base64=base64.b64encode(b"hi").decode())
    assert [row["sha256"] for row in call("list_assets")] == [asset["sha256"]]

    # Publishing lifecycle.
    assert call("get_publishing_status")["mode"] == "live"
    v1 = call("publish", message="v1")
    call("move_element", element_id=first["id"], parent_id=root["id"], position=0)
    call("publish", message="v2")
    assert [revision["number"] for revision in call("list_revisions")] == [2, 1]
    assert call("rollback", revision_id=v1["id"])["number"] == 3
    call("delete_element", element_id=second["id"])
    assert call("discard_draft")["has_unpublished_changes"] is False
    assert call("set_page_root_element", page_id=page["id"], root_element_id=None)["root_element_id"] is None
    assert call("delete_page", page_id=page["id"]) == {"deleted": page["id"]}
