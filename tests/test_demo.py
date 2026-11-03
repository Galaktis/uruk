"""``manage.py demo``: the scripted remote agent builds, publishes, and drafts a blog."""

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING

import pytest
from django.core.management import CommandError, call_command
from django.test import Client
from django_tenants.utils import tenant_context
from starlette.testclient import TestClient

from common.tenancy import tenant_connection
from content.management.commands import demo
from mcp_server.http import create_app
from services import publishing
from tenants.models import Tenant

if TYPE_CHECKING:
    from collections.abc import Iterator

HOST = {"host": demo.DEMO_DOMAIN}


@pytest.fixture
def in_process_mcp(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Route the command's HTTP calls to an in-process MCP app instead of a network socket."""
    with TestClient(create_app()) as client:

        def post_json(url: str, payload: dict, headers: dict[str, str]) -> dict:
            response = client.post("/mcp", json=payload, headers={key.lower(): value for key, value in headers.items()})
            assert response.status_code == 200, response.text
            return response.json()

        monkeypatch.setattr(demo, "post_json", post_json)
        yield


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("in_process_mcp")
def test_demo_builds_a_published_blog_with_one_draft_edit() -> None:
    out = StringIO()
    call_command("demo", "--reset", stdout=out)  # --reset: tolerate a schema left by another test
    assert "sign in as demo / " in out.getvalue()

    tenant_connection().set_schema_to_public()
    client = Client()
    home = client.get("/", headers=HOST).content.decode()
    assert "Field notes from a city of rows" in home
    assert "built in a few seconds.</p>" in home  # the published intro, not the draft
    assert home.count("<time datetime=") == 2  # "Latest": limit 2
    assert "Scribes, drafts, and publishing" in client.get("/blog", headers=HOST).content.decode()
    assert client.get("/blog/feed.xml", headers=HOST).content.decode().count("<item>") == 3
    assert "/demo.css" in home

    tenant = Tenant.objects.get(schema_name=demo.DEMO_SCHEMA)
    with tenant_context(tenant):
        status = publishing.status()
        assert status["live_revision"] is not None
        assert status["live_revision"]["message"] == "Launch the demo blog"
        assert [entry["label"] for entry in status["changes"]["elements"]["changed"]] == ["text"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("in_process_mcp")
def test_demo_refuses_to_overwrite_without_reset() -> None:
    call_command("demo", "--reset", stdout=StringIO())
    tenant_connection().set_schema_to_public()
    with pytest.raises(CommandError, match="--reset"):
        call_command("demo", stdout=StringIO())
    call_command("demo", "--reset", stdout=StringIO())
