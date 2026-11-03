"""Drafts, publishing, and revision history (§16): outside-in over HTTP, API, and MCP.

A site that has been published serves its live revision; edits go to the working copy
and stay invisible to visitors until the next publish, while signed-in editors see
them at ``/_preview/``. History is append-only (rollback republishes), and a draft can
be discarded back to any revision.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Permission, User
from django.test import Client
from django_tenants.utils import tenant_context
from ninja.testing import TestClient

from api.api import api
from content.models import Element, Page, Revision
from mcp_server import tools
from mcp_server.context import AgentContext
from services import auth as auth_svc
from services import content as svc
from services import publishing
from services.portability import archive_tenant, restore_tenant
from tenants.models import Domain, Tenant


def _site(schema: str) -> tuple[Tenant, dict[str, str], Element]:
    """Create a tenant with a routed domain and a one-text home page; return its text element."""
    tenant = Tenant.objects.create(schema_name=schema, name=schema)
    domain = f"{schema.replace('_', '-')}.localhost"  # hostnames cannot contain underscores
    Domain.objects.create(domain=domain, tenant=tenant, is_primary=True)
    with tenant_context(tenant):
        root = svc.create_element("container", parameters={"tag": "main"})
        text = svc.create_element("text", parent=root, content="v1")
        svc.create_page("index", "Home", root_element=root)
    return tenant, {"host": domain}, text


def _body(client: Client, path: str, host: dict[str, str]) -> str:
    response = client.get(path, headers=host)
    assert response.status_code == 200, response.status_code
    return response.content.decode()


@pytest.mark.django_db(transaction=True)
def test_never_published_site_serves_the_working_copy_live() -> None:
    tenant, host, text = _site("pub_live")
    with tenant_context(tenant):
        assert publishing.status()["mode"] == "live"
        svc.set_element_content(text, "edited")

    assert "<p>edited</p>" in _body(Client(), "/", host)


@pytest.mark.django_db(transaction=True)
def test_edits_stay_draft_until_published() -> None:
    tenant, host, text = _site("pub_draft")
    client = Client()
    with tenant_context(tenant):
        first = publishing.publish(message="Launch")
        svc.set_element_content(text, "v2")
        svc.create_page("draft-only", "Draft", root_element=text.parent)
        svc.set_setting("site.head_html", '<meta name="v" content="2">')

        status = publishing.status()
        assert status["mode"] == "draft"
        assert status["has_unpublished_changes"] is True
        assert status["changes"]["elements"]["changed"] == [{"id": str(text.pk), "label": "text"}]
        assert [entry["label"] for entry in status["changes"]["pages"]["added"]] == ["draft-only"]
        assert [entry["label"] for entry in status["changes"]["settings"]["added"]] == ["site.head_html"]

    # Visitors still get revision #1: old text, old head, no new page.
    home = _body(client, "/", host)
    assert "<p>v1</p>" in home
    assert 'content="2"' not in home
    assert client.get("/draft-only", headers=host).status_code == 404

    with tenant_context(tenant):
        second = publishing.publish(message="Second")
        assert publishing.status()["has_unpublished_changes"] is False
    assert (first.number, second.number) == (1, 2)

    home = _body(client, "/", host)
    assert "<p>v2</p>" in home
    assert '<meta name="v" content="2">' in home
    assert "<p>v2</p>" in _body(client, "/draft-only", host)


@pytest.mark.django_db(transaction=True)
def test_a_page_deleted_in_the_draft_stays_public_until_published() -> None:
    tenant, host, _ = _site("pub_delete")
    with tenant_context(tenant):
        publishing.publish()
        svc.delete_page(Page.objects.get(slug="index"))

    assert "<p>v1</p>" in _body(Client(), "/", host)


@pytest.mark.django_db(transaction=True)
def test_preview_shows_the_draft_to_editors_only() -> None:
    tenant, host, text = _site("pub_preview")
    with tenant_context(tenant):
        publishing.publish()
        svc.set_element_content(text, "draft")
        editor = User.objects.create_user("editor")
        editor.user_permissions.add(Permission.objects.get(codename="view_page"))
        User.objects.create_user("nobody")

    anonymous = Client()
    redirect = anonymous.get("/_preview/", headers=host)
    assert redirect.status_code == 302
    assert redirect["Location"].startswith("/uruk/login/")

    unprivileged = Client()
    with tenant_context(tenant):
        unprivileged.force_login(User.objects.get(username="nobody"))
    assert unprivileged.get("/_preview/", headers=host).status_code == 403

    client = Client()
    with tenant_context(tenant):
        client.force_login(editor)
    response = client.get("/_preview/", headers=host)
    assert response.status_code == 200
    assert "<p>draft</p>" in response.content.decode()
    assert response["Cache-Control"] == "private, no-store"
    assert response["X-Robots-Tag"] == "noindex"
    # The same editor still sees the published version on the public URL.
    assert "<p>v1</p>" in _body(client, "/", host)


@pytest.mark.django_db(transaction=True)
def test_public_fragments_serve_the_live_revision() -> None:
    tenant, host, text = _site("pub_fragment")
    with tenant_context(tenant):
        publishing.publish()
        svc.set_element_content(text, "draft")
        unpublished = svc.create_element("text", content="secret")

    client = Client()
    assert client.get(f"/_fragment/{text.pk}", headers=host).content == b"<p>v1</p>"
    assert client.get(f"/_fragment/{unpublished.pk}", headers=host).status_code == 404


@pytest.mark.django_db(transaction=True)
def test_rollback_republishes_old_content_and_keeps_history() -> None:
    tenant, host, text = _site("pub_rollback")
    with tenant_context(tenant):
        first = publishing.publish(message="v1")
        svc.set_element_content(text, "v2")
        publishing.publish(message="v2")

        third = publishing.rollback(first)
        assert third.number == 3
        assert third.message == "Rollback to #1"
        assert [r.number for r in publishing.list_revisions()] == [3, 2, 1]
        # The working copy is untouched by a rollback: it still differs from what is live.
        assert publishing.status()["has_unpublished_changes"] is True

    assert "<p>v1</p>" in _body(Client(), "/", host)


@pytest.mark.django_db(transaction=True)
def test_discard_draft_restores_the_working_copy_with_the_same_ids() -> None:
    tenant, _, text = _site("pub_discard")
    with tenant_context(tenant):
        publishing.publish()
        svc.set_element_content(text, "oops")
        svc.create_element("text", content="stray")
        svc.delete_page(Page.objects.get(slug="index"))

        publishing.discard_draft()

        assert publishing.status()["has_unpublished_changes"] is False
        assert Element.objects.get(pk=text.pk).content == "v1"
        assert not Element.objects.filter(content="stray").exists()
        assert svc.resolve_page_path("") == Page.objects.get(slug="index")


@pytest.mark.django_db(transaction=True)
def test_published_sites_stay_isolated_between_tenants() -> None:
    tenant_a, host_a, _ = _site("pub_iso_a")
    tenant_b, host_b, text_b = _site("pub_iso_b")
    with tenant_context(tenant_b):
        svc.set_element_content(text_b, "tenant b")
    for tenant in (tenant_a, tenant_b):
        with tenant_context(tenant):
            publishing.publish()

    client = Client()
    assert "<p>v1</p>" in _body(client, "/", host_a)
    assert "<p>tenant b</p>" in _body(client, "/", host_b)


@pytest.mark.django_db(transaction=True)
def test_publishing_over_the_api_is_permission_gated() -> None:
    tenant, host, text = _site("pub_api")
    with tenant_context(tenant):
        client = TestClient(api)
        editor = User.objects.create_user("editor")
        editor.user_permissions.set(Permission.objects.filter(content_type__app_label="content").exclude(codename="add_revision"))
        assert client.post("/publishing/publish", json={"message": "nope"}, user=editor).status_code == 403

        admin = User.objects.create_superuser("admin", "admin@example.com", "pw")
        published = client.post("/publishing/publish", json={"message": "Launch"}, user=admin)
        assert published.status_code == 201
        assert published.json()["number"] == 1
        assert published.json()["author"] == "admin"

        svc.set_element_content(text, "v2")
        status = client.get("/publishing", user=editor).json()
        assert status["mode"] == "draft"
        assert status["has_unpublished_changes"] is True

        client.post("/publishing/publish", json={"message": "v2"}, user=admin)
        revisions = client.get("/revisions", user=editor).json()
        assert [r["message"] for r in revisions] == ["v2", "Launch"]

        rolled = client.post(f"/revisions/{revisions[1]['id']}/rollback", user=admin)
        assert rolled.status_code == 201
        assert rolled.json()["message"] == "Rollback to #1"

        discarded = client.post("/publishing/discard", json={}, user=editor)
        assert discarded.status_code == 200
        assert discarded.json()["has_unpublished_changes"] is False
        assert Element.objects.get(pk=text.pk).content == "v1"

    assert "<p>v1</p>" in _body(Client(), "/", host)


@pytest.mark.django_db(transaction=True)
def test_agent_publishes_over_mcp() -> None:
    tenant, host, text = _site("pub_mcp")
    with tenant_context(tenant):
        agent = User.objects.create_user("agent")
        agent.user_permissions.set(Permission.objects.filter(content_type__app_label="content"))
        _, token = auth_svc.create_token(agent)
    ctx = AgentContext(tenant=tenant, token=token)

    assert tools.get_publishing_status(ctx)["mode"] == "live"
    revision = tools.publish(ctx, "First")
    assert revision["author"] == "agent"

    tools.update_element(ctx, str(text.pk), content="agent draft")
    assert "agent draft" in tools.render_page(ctx, _page_id(tenant))
    assert "<p>v1</p>" in _body(Client(), "/", host)

    tools.publish(ctx, "Second")
    assert "<p>agent draft</p>" in _body(Client(), "/", host)
    assert [r["message"] for r in tools.list_revisions(ctx)] == ["Second", "First"]

    tools.rollback(ctx, revision["id"])
    tools.discard_draft(ctx)
    assert tools.get_publishing_status(ctx)["has_unpublished_changes"] is False
    assert "<p>v1</p>" in _body(Client(), "/", host)


def _page_id(tenant: Tenant) -> str:
    with tenant_context(tenant):
        return str(Page.objects.get(slug="index").pk)


@pytest.mark.django_db(transaction=True)
def test_history_travels_with_an_archive() -> None:
    source, _, text = _site("pub_arch_src")
    with tenant_context(source):
        publishing.publish(message="v1")
        svc.set_element_content(text, "v2")
        publishing.publish(message="v2")
        svc.set_element_content(text, "unpublished")

    archive = archive_tenant(source)
    assert archive["uruk_archive_version"] == 1

    restored = restore_tenant(archive, schema_name="pub_arch_dst")
    Domain.objects.create(domain="pub-arch-dst.localhost", tenant=restored, is_primary=True)
    with tenant_context(restored):
        assert [(r.number, r.message) for r in Revision.objects.all()] == [(2, "v2"), (1, "v1")]
        assert Element.objects.get(pk=text.pk).content == "unpublished"
    assert "<p>v2</p>" in _body(Client(), "/", {"host": "pub-arch-dst.localhost"})


@pytest.mark.django_db(transaction=True)
def test_restoring_an_unpublished_archive_publishes_it_once() -> None:
    source, _, _ = _site("pub_nohist_src")
    archive = archive_tenant(source)
    del archive["revisions"]  # a hand-built archive, with no history

    restored = restore_tenant(archive, schema_name="pub_nohist_dst")
    with tenant_context(restored):
        assert [r.message for r in Revision.objects.all()] == ["Restored from archive"]
        assert publishing.status()["has_unpublished_changes"] is False
