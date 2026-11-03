"""API token authentication + the platform-admin gate (ARCHITECTURE.md §4/§11)."""

from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import AnonymousUser, Permission, User
from django.core.management import call_command
from django_tenants.utils import tenant_context

from content.models import ApiToken
from services import auth as auth_svc
from tenants import default as default_tenant
from tenants.models import Domain, Tenant


def _tenant_with_domain(schema: str, host: str) -> Tenant:
    tenant = Tenant.objects.create(schema_name=schema, name=schema)
    Domain.objects.create(domain=host, tenant=tenant, is_primary=True)
    return tenant


def _tenant_user(*, with_perms: bool) -> User:
    user = User.objects.create_user("editor")
    if with_perms:
        user.user_permissions.set(Permission.objects.filter(content_type__app_label="content"))
    return user


# HTTP API auth ---------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_missing_token_is_401(client) -> None:  # noqa: ANN001 - pytest-django fixture
    _tenant_with_domain("auth_anon", "anon.localhost")
    response = client.get("/api/pages", headers={"host": "anon.localhost"})
    assert response.status_code == 401


@pytest.mark.django_db(transaction=True)
def test_valid_token_with_permission_succeeds(client) -> None:  # noqa: ANN001
    tenant = _tenant_with_domain("auth_ok", "ok.localhost")
    with tenant_context(tenant):
        _, token = auth_svc.create_token(_tenant_user(with_perms=True))

    response = client.get("/api/pages", headers={"host": "ok.localhost", "authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.django_db(transaction=True)
def test_valid_token_without_permission_is_403(client) -> None:  # noqa: ANN001
    tenant = _tenant_with_domain("auth_noperm", "noperm.localhost")
    with tenant_context(tenant):
        _, token = auth_svc.create_token(_tenant_user(with_perms=False))

    response = client.post(
        "/api/elements",
        data=json.dumps({"kind": "text"}),
        content_type="application/json",
        headers={"host": "noperm.localhost", "authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


@pytest.mark.django_db(transaction=True)
def test_token_is_isolated_to_its_tenant(client) -> None:  # noqa: ANN001
    tenant_a = _tenant_with_domain("auth_iso_a", "iso-a.localhost")
    _tenant_with_domain("auth_iso_b", "iso-b.localhost")
    with tenant_context(tenant_a):
        _, token = auth_svc.create_token(_tenant_user(with_perms=True))

    # A's token works against A...
    assert client.get("/api/pages", headers={"host": "iso-a.localhost", "authorization": f"Bearer {token}"}).status_code == 200
    # ...but is unknown in B's schema, so it authenticates no one there.
    assert client.get("/api/pages", headers={"host": "iso-b.localhost", "authorization": f"Bearer {token}"}).status_code == 401


# Token service ---------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_resolve_token_stores_only_a_hash_and_tracks_use() -> None:
    tenant = Tenant.objects.create(schema_name="auth_svc", name="svc")
    with tenant_context(tenant):
        user = _tenant_user(with_perms=False)
        token_obj, raw = auth_svc.create_token(user)

        # The plaintext is never stored; only its hash is.
        assert token_obj.key_hash != raw
        assert token_obj.last_used is None

        resolved = auth_svc.resolve_token(raw)
        assert resolved == user
        assert auth_svc.resolve_token("uruk_not-a-real-token") is None
        assert auth_svc.resolve_token(None) is None

        token_obj.refresh_from_db()
        assert token_obj.last_used is not None


@pytest.mark.django_db(transaction=True)
def test_revoked_token_no_longer_resolves() -> None:
    tenant = Tenant.objects.create(schema_name="auth_revoke", name="revoke")
    with tenant_context(tenant):
        token_obj, raw = auth_svc.create_token(_tenant_user(with_perms=False))
        auth_svc.revoke_token(token_obj)
        assert auth_svc.resolve_token(raw) is None
        assert not ApiToken.objects.exists()


# Platform admin --------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_platform_admin_gate_requires_default_tenant_superuser(monkeypatch) -> None:  # noqa: ANN001 - pytest fixture
    monkeypatch.setattr(default_tenant, "DEFAULT_TENANT_SCHEMA", "auth_platform")
    default = Tenant.objects.create(schema_name="auth_platform", name="Default")
    with tenant_context(default):
        superuser = User.objects.create_superuser("platform", "p@example.com", "x")
        plain = User.objects.create_user("plain")

    assert auth_svc.is_platform_admin(superuser) is True
    assert auth_svc.is_platform_admin(plain) is False
    assert auth_svc.is_platform_admin(AnonymousUser()) is False

    auth_svc.ensure_platform_admin(superuser)  # does not raise
    with pytest.raises(auth_svc.PlatformPermissionError):
        auth_svc.ensure_platform_admin(plain)


# Bootstrap command -----------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_create_api_token_command_issues_a_working_token(capsys) -> None:  # noqa: ANN001 - pytest fixture
    tenant = Tenant.objects.create(schema_name="auth_cmd", name="cmd")
    with tenant_context(tenant):
        _tenant_user(with_perms=True)

    call_command("create_api_token", "auth_cmd", "editor")
    # Creating the tenant above may emit schema-migration output; the token is the
    # command's own final line.
    printed = capsys.readouterr().out.strip().splitlines()[-1].strip()

    assert printed.startswith("uruk_")
    with tenant_context(tenant):
        assert auth_svc.resolve_token(printed) is not None
