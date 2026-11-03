"""Studio, the htmx review UI: a signed-in human reviews, edits, and publishes.

Driven like a browser running htmx would: plain GET/POST with the session cookie and
the CSRF header, asserting on the returned fragments, the ``HX-Trigger`` event that
refreshes the other panes, and the resulting DB state and public site.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Permission, User
from django.test import Client
from django_tenants.utils import tenant_context

from common.tenancy import tenant_connection
from content.models import Element, Revision
from services import content as svc
from services import publishing
from tenants.models import Domain, Tenant


@pytest.fixture(autouse=True)
def _plain_staticfiles(settings) -> None:  # noqa: ANN001 - pytest-django fixture
    """Resolve ``{% static %}`` without a collectstatic manifest (tests run with ``DEBUG=False``)."""
    settings.STORAGES = {**settings.STORAGES, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}


def _site(schema: str, *, perms: list[str] | None = None) -> tuple[Tenant, dict[str, str], Element, Client]:
    """Create a published one-page site and a client signed in as an editor (all content perms by default)."""
    tenant_connection().set_schema_to_public()
    tenant = Tenant.objects.create(schema_name=schema, name=schema)
    domain = f"{schema.replace('_', '-')}.localhost"
    Domain.objects.create(domain=domain, tenant=tenant, is_primary=True)
    client = Client()
    with tenant_context(tenant):
        root = svc.create_element("container", parameters={"tag": "main"})
        text = svc.create_element("text", parent=root, content="Hello")
        svc.create_element("text", parent=root, position=1, content="World")
        svc.create_page("index", "Home", root_element=root)
        publishing.publish(message="Launch")
        editor = User.objects.create_user("editor")
        permissions = Permission.objects.filter(content_type__app_label="content")
        if perms is not None:
            permissions = permissions.filter(codename__in=perms)
        editor.user_permissions.set(permissions)
        client.force_login(editor)
    return tenant, {"host": domain}, text, client


@pytest.mark.django_db(transaction=True)
def test_studio_requires_a_signed_in_editor() -> None:
    _, host, _, _ = _site("studio_auth")
    response = Client().get("/_studio/", headers=host)
    assert response.status_code == 302
    assert response["Location"].startswith("/uruk/login/")

    _, host, _, viewer = _site("studio_noperm", perms=[])
    assert viewer.get("/_studio/", headers=host).status_code == 403


@pytest.mark.django_db(transaction=True)
def test_studio_shows_pages_elements_status_and_the_draft_preview() -> None:
    _, host, _, client = _site("studio_home")
    body = client.get("/_studio/", headers=host).content.decode()

    assert "htmx.min.js" in body
    assert ">Home</a>" in body
    assert 'src="/_preview/"' in body
    assert "Live: #1" in body
    assert "Up to date" in body
    assert '<span class="text">Hello</span>' in body

    # The framed preview must be allowed to render inside the studio.
    preview = client.get("/_preview/", headers=host)
    assert preview["X-Frame-Options"] == "SAMEORIGIN"


@pytest.mark.django_db(transaction=True)
def test_editing_an_element_saves_to_the_draft_then_publish_makes_it_live() -> None:
    tenant, host, text, client = _site("studio_edit")

    editor = client.get(f"/_studio/elements/{text.pk}", headers=host)
    assert editor.status_code == 200
    assert "Hello</textarea>" in editor.content.decode()

    saved = client.post(f"/_studio/elements/{text.pk}", {"content": "Hello, edited", "parameters": "{}"}, headers=host)
    assert saved.status_code == 200
    assert saved["HX-Trigger"] == "content-changed"
    assert "Saved to the draft" in saved.content.decode()
    with tenant_context(tenant):
        assert Element.objects.get(pk=text.pk).content == "Hello, edited"

    status = client.get("/_studio/status", headers=host).content.decode()
    assert "1 unpublished change" in status
    assert "<p>Hello</p>" in Client().get("/", headers=host).content.decode()  # visitors: still v1

    published = client.post("/_studio/publish", {"message": "Edited greeting"}, headers=host)
    assert published["HX-Trigger"] == "content-changed"
    assert "Live: #2 — Edited greeting" in published.content.decode()
    assert "<p>Hello, edited</p>" in Client().get("/", headers=host).content.decode()


@pytest.mark.django_db(transaction=True)
def test_invalid_parameters_are_reported_not_saved() -> None:
    tenant, host, text, client = _site("studio_json")
    response = client.post(f"/_studio/elements/{text.pk}", {"content": "x", "parameters": "{not json"}, headers=host)
    assert "not valid JSON" in response.content.decode()
    assert "HX-Trigger" not in response
    with tenant_context(tenant):
        assert Element.objects.get(pk=text.pk).content == "Hello"


@pytest.mark.django_db(transaction=True)
def test_move_delete_discard_and_rollback() -> None:
    tenant, host, text, client = _site("studio_ops")

    client.post(f"/_studio/elements/{text.pk}/move", {"direction": "down"}, headers=host)
    with tenant_context(tenant):
        assert [child.content for child in Element.objects.get(pk=text.parent_id).children.all()] == ["World", "Hello"]

    client.post(f"/_studio/elements/{text.pk}/delete", headers=host)
    with tenant_context(tenant):
        assert not Element.objects.filter(pk=text.pk).exists()

    discarded = client.post("/_studio/discard", headers=host)
    assert "Up to date" in discarded.content.decode()
    with tenant_context(tenant):
        assert Element.objects.get(pk=text.pk).content == "Hello"
        svc.set_element_content(text, "v2")
        publishing.publish(message="v2")
        first = Revision.objects.get(number=1)

    rolled = client.post(f"/_studio/revisions/{first.pk}/rollback", headers=host)
    assert "Live: #3 — Rollback to #1" in rolled.content.decode()
    assert "<p>Hello</p>" in Client().get("/", headers=host).content.decode()


@pytest.mark.django_db(transaction=True)
def test_each_action_checks_its_own_permission() -> None:
    _, host, text, client = _site("studio_ro", perms=["view_page", "view_element", "view_revision"])

    editor = client.get(f"/_studio/elements/{text.pk}", headers=host).content.decode()
    assert "readonly" in editor
    assert client.post(f"/_studio/elements/{text.pk}", {"content": "x", "parameters": "{}"}, headers=host).status_code == 403
    assert client.post(f"/_studio/elements/{text.pk}/delete", headers=host).status_code == 403
    assert client.post("/_studio/publish", headers=host).status_code == 403
    assert client.post("/_studio/discard", headers=host).status_code == 403
