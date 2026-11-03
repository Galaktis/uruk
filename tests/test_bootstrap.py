"""Autonomous bootstrap: eager default-tenant provisioning, one-shot claim, JWT, tenant creation.

These exercise the "start it with only DB credentials" path: the ``bootstrap`` command
eagerly provisions the default tenant (which the fallback middleware also serves —
covered in ``test_default_tenant.py``); ``POST /api/bootstrap`` claims its first superuser
(with the tenant's claim code) and returns a set-password link; JWT login/refresh authenticate humans; and only the
default-tenant superuser may create further tenants.

Each test that touches the default tenant patches ``tenants.default.DEFAULT_TENANT_SCHEMA``
to a unique name so the created schema (and its users) do not bleed across tests.
"""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth.models import Permission, User
from django.core.management import call_command
from django.core.management.base import CommandError
from django_tenants.utils import schema_exists, tenant_context

from content.models import Page
from services import auth as auth_svc
from services import jwt as jwt_svc
from services.auth import claim_code
from tenants import default as default_tenant
from tenants.models import Domain, Tenant

if TYPE_CHECKING:
    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse


def _default_tenant(schema: str, host: str) -> Tenant:
    """Create a default-style tenant + its (exact-match) domain for the test."""
    tenant = Tenant.objects.create(schema_name=schema, name="Default")
    Domain.objects.create(domain=host, tenant=tenant, is_primary=True)
    return tenant


def _post(client: Client, path: str, body: dict, *, host: str, auth: str | None = None) -> _MonkeyPatchedWSGIResponse:
    """POST JSON to ``path`` with a Host header and optional Bearer token."""
    headers = {"host": host}
    if auth is not None:
        headers["authorization"] = f"Bearer {auth}"
    return client.post(path, data=json.dumps(body), content_type="application/json", headers=headers)


# bootstrap command -----------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_bootstrap_command_provisions_default_tenant(monkeypatch) -> None:  # noqa: ANN001 - pytest fixture
    monkeypatch.setattr(default_tenant, "DEFAULT_TENANT_SCHEMA", "boot_cmd")
    monkeypatch.setattr(default_tenant, "DEFAULT_TENANT_DOMAIN", "boot-cmd.localhost")

    out = io.StringIO()
    call_command("bootstrap", stdout=out)
    call_command("bootstrap")  # second run must be a no-op, not an error
    assert f"code {claim_code('boot_cmd')}" in out.getvalue()  # the operator reads the claim code in the logs

    tenants = Tenant.objects.filter(schema_name="boot_cmd")
    assert tenants.count() == 1
    assert Domain.objects.filter(tenant=tenants.first(), is_primary=True).exists()


# One-shot claim + set-password ----------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_claim_returns_link_then_refuses_second_claim(client, settings) -> None:  # noqa: ANN001
    settings.URUK_ADMIN_EMAIL = "admin@example.com"
    tenant = _default_tenant("boot_claim", "claim.localhost")

    # Wrong email is refused.
    assert _post(client, "/api/bootstrap", {"email": "someone@else.com", "code": claim_code("boot_claim")}, host="claim.localhost").status_code == 403

    # The configured admin claims the tenant and gets a set-password link.
    ok = _post(client, "/api/bootstrap", {"email": "admin@example.com", "code": claim_code("boot_claim")}, host="claim.localhost")
    assert ok.status_code == 200
    url = ok.json()["set_password_url"]
    assert url.startswith("/api/set-password?")

    # A second claim is refused now that a superuser exists.
    assert _post(client, "/api/bootstrap", {"email": "admin@example.com", "code": claim_code("boot_claim")}, host="claim.localhost").status_code == 403

    # Redeeming the link sets a working password.
    query = parse_qs(urlparse(url).query)
    redeem = _post(
        client,
        "/api/set-password",
        {"uid": query["uid"][0], "token": query["token"][0], "password": "Sup3rS3cret!pw"},
        host="claim.localhost",
    )
    assert redeem.status_code == 200

    with tenant_context(tenant):
        user = User.objects.get(username="admin@example.com")
        assert user.is_superuser
        assert user.check_password("Sup3rS3cret!pw")


@pytest.mark.django_db(transaction=True)
def test_claim_requires_the_tenant_claim_code(client, settings) -> None:  # noqa: ANN001
    """The link goes to whoever asks, so the claim needs the server-side code — even with no admin email set."""
    settings.URUK_ADMIN_EMAIL = ""
    tenant = _default_tenant("boot_flag", "flag.localhost")
    code = claim_code("boot_flag")

    # A stranger without the code is refused, as is another tenant's code.
    assert _post(client, "/api/bootstrap", {"email": "first@example.com", "code": "guess"}, host="flag.localhost").status_code == 403
    other_code = claim_code("other")
    assert _post(client, "/api/bootstrap", {"email": "first@example.com", "code": other_code}, host="flag.localhost").status_code == 403
    with tenant_context(tenant):
        assert not User.objects.exists()

    # With the code, any address may claim — but only until a superuser exists.
    ok = _post(client, "/api/bootstrap", {"email": "first@example.com", "code": code}, host="flag.localhost")
    assert ok.status_code == 200
    with tenant_context(tenant):
        assert User.objects.get(username="first@example.com").is_superuser

    # Every later claim is refused — including from the same address.
    assert _post(client, "/api/bootstrap", {"email": "second@example.com", "code": code}, host="flag.localhost").status_code == 403
    assert _post(client, "/api/bootstrap", {"email": "first@example.com", "code": code}, host="flag.localhost").status_code == 403


@pytest.mark.django_db(transaction=True)
def test_set_password_rejects_a_bad_token(client, settings) -> None:  # noqa: ANN001
    settings.URUK_ADMIN_EMAIL = "admin@example.com"
    _default_tenant("boot_badtoken", "badtoken.localhost")
    claim = {"email": "admin@example.com", "code": claim_code("boot_badtoken")}
    url = _post(client, "/api/bootstrap", claim, host="badtoken.localhost").json()["set_password_url"]
    query = parse_qs(urlparse(url).query)

    redeem = _post(
        client,
        "/api/set-password",
        {"uid": query["uid"][0], "token": "not-the-real-token", "password": "Sup3rS3cret!pw"},
        host="badtoken.localhost",
    )
    assert redeem.status_code == 400


# JWT login / refresh ---------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_jwt_login_and_refresh_flow(client) -> None:  # noqa: ANN001
    tenant = _default_tenant("boot_jwt", "jwt.localhost")
    with tenant_context(tenant):
        user = User.objects.create_user("editor", "e@x.com", "correct-horse")
        user.user_permissions.set(Permission.objects.filter(content_type__app_label="content"))

    # Bad credentials are rejected.
    assert _post(client, "/api/auth/login", {"username": "editor", "password": "wrong"}, host="jwt.localhost").status_code == 401

    # Good credentials return an access + refresh pair.
    login = _post(client, "/api/auth/login", {"username": "editor", "password": "correct-horse"}, host="jwt.localhost")
    assert login.status_code == 200
    access, refresh = login.json()["access"], login.json()["refresh"]

    # The access token authorizes an API call.
    listed = client.get("/api/pages", headers={"host": "jwt.localhost", "authorization": f"Bearer {access}"})
    assert listed.status_code == 200

    # Refresh rotates to a new, working access token.
    rotated = _post(client, "/api/auth/refresh", {"refresh": refresh}, host="jwt.localhost")
    assert rotated.status_code == 200
    new_access = rotated.json()["access"]
    assert client.get("/api/pages", headers={"host": "jwt.localhost", "authorization": f"Bearer {new_access}"}).status_code == 200

    # A refresh token is not accepted as an access token.
    assert client.get("/api/pages", headers={"host": "jwt.localhost", "authorization": f"Bearer {refresh}"}).status_code == 401


@pytest.mark.django_db(transaction=True)
def test_api_key_still_authenticates_alongside_jwt(client) -> None:  # noqa: ANN001
    """The MCP/machine surface (uruk_ token) must keep working next to JWT."""
    tenant = _default_tenant("boot_apikey", "apikey.localhost")
    with tenant_context(tenant):
        user = User.objects.create_user("bot", "b@x.com")
        user.user_permissions.set(Permission.objects.filter(content_type__app_label="content"))
        _, raw = auth_svc.create_token(user)

    assert raw.startswith("uruk_")
    listed = client.get("/api/pages", headers={"host": "apikey.localhost", "authorization": f"Bearer {raw}"})
    assert listed.status_code == 200


# Tenant creation gate --------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_only_default_superuser_can_create_tenants(client, monkeypatch) -> None:  # noqa: ANN001
    # The gate checks superuser-ness in DEFAULT_TENANT_SCHEMA; point it at this
    # test's tenant so its superuser is recognised as the platform admin.
    monkeypatch.setattr(default_tenant, "DEFAULT_TENANT_SCHEMA", "boot_platform")
    tenant = _default_tenant("boot_platform", "platform.localhost")
    with tenant_context(tenant):
        superuser = User.objects.create_superuser("platform", "p@x.com", "pw")
        plain = User.objects.create_user("plain", "plain@x.com", "pw")
        admin_access, _ = jwt_svc.issue_pair(superuser)
        plain_access, _ = jwt_svc.issue_pair(plain)

    payload = {"schema_name": "boot_acme", "name": "Acme", "domain": "acme.localhost"}

    # A non-superuser is forbidden.
    assert _post(client, "/api/tenants", payload, host="platform.localhost", auth=plain_access).status_code == 403

    # The default-tenant superuser succeeds.
    created = _post(client, "/api/tenants", payload, host="platform.localhost", auth=admin_access)
    assert created.status_code == 201
    assert created.json()["schema_name"] == "boot_acme"
    assert created.json()["claim_code"] == claim_code("boot_acme")  # handed to the new site's owner
    assert Tenant.objects.filter(schema_name="boot_acme").exists()
    assert Domain.objects.filter(domain="acme.localhost", is_primary=True).exists()


# Tenant deletion gate ----------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_only_default_superuser_can_delete_tenants(client, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(default_tenant, "DEFAULT_TENANT_SCHEMA", "boot_del_platform")
    tenant = _default_tenant("boot_del_platform", "delplatform.localhost")
    with tenant_context(tenant):
        superuser = User.objects.create_superuser("platform", "p@x.com", "pw")
        plain = User.objects.create_user("plain", "plain@x.com", "pw")
        admin_access, _ = jwt_svc.issue_pair(superuser)
        plain_access, _ = jwt_svc.issue_pair(plain)

    victim = Tenant.objects.create(schema_name="boot_del_victim", name="Victim")
    Domain.objects.create(domain="victim.localhost", tenant=victim, is_primary=True)
    assert schema_exists("boot_del_victim")

    def _delete(schema: str, auth: str):  # noqa: ANN202
        return client.delete(f"/api/tenants/{schema}", headers={"host": "delplatform.localhost", "authorization": f"Bearer {auth}"})

    # A non-superuser is forbidden; the tenant survives.
    assert _delete("boot_del_victim", plain_access).status_code == 403
    assert Tenant.objects.filter(schema_name="boot_del_victim").exists()

    # The platform admin deletes it: row, domain, and schema all gone.
    assert _delete("boot_del_victim", admin_access).status_code == 204
    assert not Tenant.objects.filter(schema_name="boot_del_victim").exists()
    assert not Domain.objects.filter(domain="victim.localhost").exists()
    assert not schema_exists("boot_del_victim")

    # The default tenant is protected over the API; unknown schemas 404.
    assert _delete("boot_del_platform", admin_access).status_code == 400
    assert Tenant.objects.filter(schema_name="boot_del_platform").exists()
    assert _delete("no_such_schema", admin_access).status_code == 404


@pytest.mark.django_db(transaction=True)
def test_reset_tenants_command_drops_everything_and_reseeds_the_default(monkeypatch) -> None:  # noqa: ANN001 - pytest fixture
    monkeypatch.setattr(default_tenant, "DEFAULT_TENANT_SCHEMA", "reset_default")
    monkeypatch.setattr(default_tenant, "DEFAULT_TENANT_DOMAIN", "reset.localhost")
    call_command("bootstrap")
    Tenant.objects.create(schema_name="reset_extra", name="Extra")
    assert schema_exists("reset_extra")

    # Without --yes nothing happens.
    with pytest.raises(CommandError, match="--yes"):
        call_command("reset_tenants")
    assert Tenant.objects.filter(schema_name="reset_extra").exists()

    call_command("reset_tenants", "--yes")

    # Every tenant is gone; the default has been recreated and reseeded.
    assert not Tenant.objects.filter(schema_name="reset_extra").exists()
    assert not schema_exists("reset_extra")
    fresh_default = Tenant.objects.get(schema_name="reset_default")
    with tenant_context(fresh_default):
        assert Page.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_delete_tenant_command_requires_confirmation_then_drops_schema() -> None:
    Tenant.objects.create(schema_name="cmd_del", name="CmdDel")
    assert schema_exists("cmd_del")

    # Without --yes nothing happens.
    with pytest.raises(CommandError, match="--yes"):
        call_command("remove_tenant", "cmd_del")
    assert Tenant.objects.filter(schema_name="cmd_del").exists()

    call_command("remove_tenant", "cmd_del", "--yes")
    assert not Tenant.objects.filter(schema_name="cmd_del").exists()
    assert not schema_exists("cmd_del")

    with pytest.raises(CommandError, match="No tenant"):
        call_command("remove_tenant", "cmd_del", "--yes")


# claim_code / ensure_default_tenant commands ----------------------------------
@pytest.mark.django_db(transaction=True)
def test_claim_code_command_prints_the_tenant_code() -> None:
    Tenant.objects.create(schema_name="claim_cmd", name="Claim")
    out = io.StringIO()
    call_command("claim_code", "claim_cmd", stdout=out)
    assert out.getvalue().strip() == claim_code("claim_cmd")

    with pytest.raises(CommandError, match="No tenant with schema 'nope'"):
        call_command("claim_code", "nope")


@pytest.mark.django_db(transaction=True)
def test_ensure_default_tenant_command_is_idempotent(monkeypatch) -> None:  # noqa: ANN001 - pytest fixture
    monkeypatch.setattr(default_tenant, "DEFAULT_TENANT_SCHEMA", "ensure_cmd")
    monkeypatch.setattr(default_tenant, "DEFAULT_TENANT_DOMAIN", "ensure-cmd.localhost")

    first, second = io.StringIO(), io.StringIO()
    call_command("ensure_default_tenant", stdout=first)
    call_command("ensure_default_tenant", stdout=second)
    assert "Default tenant created: schema 'ensure_cmd'" in first.getvalue()
    assert "Default tenant already present: schema 'ensure_cmd'" in second.getvalue()
    assert Tenant.objects.filter(schema_name="ensure_cmd").count() == 1
