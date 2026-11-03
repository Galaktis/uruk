"""Outside-in admin API tests: drive endpoints, assert DB + rendered output (§12)."""

import base64

import pytest
from django.contrib.auth.models import AnonymousUser, Permission, User
from django_tenants.utils import tenant_context
from ninja.testing import TestClient

from api.api import api
from content.models import Element, Page
from tenants.models import Tenant


def _user_with_content_perms() -> User:
    user = User.objects.create_user("admin")
    user.user_permissions.set(Permission.objects.filter(content_type__app_label="content"))
    return user


@pytest.mark.django_db(transaction=True)
def test_build_and_render_page_through_api() -> None:
    tenant = Tenant.objects.create(schema_name="api_build", name="Api")

    with tenant_context(tenant):
        user = _user_with_content_perms()
        client = TestClient(api)

        root = client.post("/elements", json={"kind": "container", "parameters": {"tag": "section"}}, user=user)
        assert root.status_code == 201
        root_id = root.json()["id"]

        child = client.post("/elements", json={"kind": "text", "parent_id": root_id, "content": "Hi"}, user=user)
        assert child.status_code == 201

        page = client.post("/pages", json={"slug": "index", "name": "Index", "root_element_id": root_id}, user=user)
        assert page.status_code == 201
        page_id = page.json()["id"]

        # DB state reflects the API calls.
        assert Page.objects.filter(slug="index").count() == 1
        assert Element.objects.filter(parent_id=root_id).count() == 1

        # Rendered output via the API closes the edit loop.
        rendered = client.get(f"/pages/{page_id}/render", user=user)
        assert rendered.status_code == 200
        assert rendered.content.decode() == "<section><p>Hi</p></section>"


@pytest.mark.django_db(transaction=True)
def test_update_and_delete_through_api() -> None:
    tenant = Tenant.objects.create(schema_name="api_edit", name="ApiEdit")

    with tenant_context(tenant):
        user = _user_with_content_perms()
        client = TestClient(api)
        element = client.post("/elements", json={"kind": "text", "content": "a"}, user=user).json()

        patched = client.patch(f"/elements/{element['id']}", json={"content": "b"}, user=user)
        assert patched.status_code == 200
        assert patched.json()["content"] == "b"

        deleted = client.delete(f"/elements/{element['id']}", user=user)
        assert deleted.status_code == 204
        assert not Element.objects.filter(pk=element["id"]).exists()


@pytest.mark.django_db(transaction=True)
def test_authorization_enforced() -> None:
    tenant = Tenant.objects.create(schema_name="api_perm", name="ApiPerm")

    with tenant_context(tenant):
        client = TestClient(api)

        anonymous = client.post("/elements", json={"kind": "text"}, user=AnonymousUser())
        assert anonymous.status_code == 401

        no_perm = User.objects.create_user("noperm")
        forbidden = client.post("/elements", json={"kind": "text"}, user=no_perm)
        assert forbidden.status_code == 403


@pytest.mark.django_db(transaction=True)
def test_settings_and_assets_through_api() -> None:
    tenant = Tenant.objects.create(schema_name="api_misc", name="ApiMisc")

    with tenant_context(tenant):
        user = _user_with_content_perms()
        client = TestClient(api)

        setting = client.put("/settings/site.title", json={"value": {"text": "Uruk"}}, user=user)
        assert setting.status_code == 200
        assert setting.json() == {"key": "site.title", "value": {"text": "Uruk"}}
        assert client.get("/settings", user=user).json() == {"site.title": {"text": "Uruk"}}

        payload = {"name": "main.css", "content_type": "text/css", "data_base64": base64.b64encode(b"body{}").decode()}
        asset = client.post("/assets", json=payload, user=user)
        assert asset.status_code == 201
        assert len(asset.json()["sha256"]) == 64


@pytest.mark.django_db(transaction=True)
def test_page_and_element_read_update_delete_through_api() -> None:
    tenant = Tenant.objects.create(schema_name="api_pages", name="ApiPages")

    with tenant_context(tenant):
        user = _user_with_content_perms()
        client = TestClient(api)
        element = client.post("/elements", json={"kind": "text", "content": "x"}, user=user).json()
        page = client.post("/pages", json={"slug": "about", "name": "About", "root_element_id": element["id"]}, user=user).json()

        assert client.get(f"/elements/{element['id']}", user=user).json()["content"] == "x"
        assert client.get(f"/pages/{page['id']}", user=user).json()["slug"] == "about"
        patched = client.patch(f"/pages/{page['id']}", json={"name": "About us", "parameters": {"noindex": True}}, user=user)
        assert patched.json()["name"] == "About us"
        assert Page.objects.get(pk=page["id"]).parameters == {"noindex": True}

        assert client.delete(f"/pages/{page['id']}", user=user).status_code == 204
        assert not Page.objects.filter(pk=page["id"]).exists()
        assert client.get(f"/pages/{page['id']}", user=user).status_code == 404


@pytest.mark.django_db(transaction=True)
def test_bad_credentials_and_tokens_are_refused() -> None:
    tenant = Tenant.objects.create(schema_name="api_creds", name="ApiCreds")

    with tenant_context(tenant):
        User.objects.create_user("editor", password="a-long-enough-password")  # noqa: S106 - test credential
        client = TestClient(api)

        assert client.post("/auth/login", json={"username": "editor", "password": "wrong"}).status_code == 401
        assert client.post("/auth/refresh", json={"refresh": "not-a-jwt"}).status_code == 401
        invalid_link = client.post("/set-password", json={"uid": "nope", "token": "nope", "password": "a-long-enough-password"})
        assert invalid_link.status_code == 400
