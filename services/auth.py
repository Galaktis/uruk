"""Authentication services: tenant API tokens and the platform-admin gate.

Two authority levels live here (ARCHITECTURE.md §4/§11):

- **Tenant tokens** — an :class:`~content.models.ApiToken` in the tenant schema
  authenticates an API/MCP client *as a tenant user*. Authorization is then that
  user's ordinary Django permissions; there is no parallel permission system.
- **Platform admin** — tenant lifecycle operations (create/delete/archive/restore) require
  an ``is_superuser`` user in the **default tenant** schema. That tenant is the one
  every unmatched host routes to, so its superuser is the natural platform operator;
  this avoids maintaining a separate identity in the public schema.

Callers of the tenant-token functions must already be inside the tenant schema
(``DefaultTenantFallbackMiddleware`` does this per request; tests use ``tenant_context``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django_tenants.utils import schema_context

from common.tenancy import current_schema
from content.models import ApiToken, hash_token
from tenants import default as default_tenant

if TYPE_CHECKING:
    from django.contrib.auth.models import AnonymousUser


class PlatformPermissionError(PermissionError):
    """Raised when a platform-admin-only operation is attempted without a superuser."""


class AlreadyClaimedError(Exception):
    """Raised when a tenant's first superuser is claimed but one already exists."""


class ClaimNotAllowedError(Exception):
    """Raised when a claim is attempted with an email that is not the configured admin."""


class InvalidSetPasswordError(Exception):
    """Raised when a set-password link's uid/token pair is invalid or expired."""


# Tenant API tokens -----------------------------------------------------------
def create_token(user: User, *, name: str = "") -> tuple[ApiToken, str]:
    """Issue an API token for a tenant user; return it and its one-time plaintext."""
    return ApiToken.generate(user, name=name)


def resolve_token(raw: str | None) -> User | None:
    """Return the tenant user a raw token authenticates as, or ``None`` if invalid.

    Runs against the currently-active tenant schema; a token from another tenant's
    schema simply will not be found here, so tokens are isolated per site.
    """
    if not raw:
        return None
    token = ApiToken.objects.filter(key_hash=hash_token(raw)).select_related("user").first()
    if token is None:
        return None
    ApiToken.objects.filter(pk=token.pk).update(last_used=timezone.now())
    return token.user


def revoke_token(token: ApiToken) -> None:
    """Delete a token, immediately invalidating it."""
    token.delete()


# Platform admin --------------------------------------------------------------
def is_platform_admin(user: User | AnonymousUser | None) -> bool:
    """Return whether ``user`` is a superuser in the default-tenant schema."""
    with schema_context(default_tenant.DEFAULT_TENANT_SCHEMA):
        return bool(user is not None and user.is_authenticated and user.is_superuser)


def ensure_platform_admin(user: User | AnonymousUser | None) -> None:
    """Raise :class:`PlatformPermissionError` unless ``user`` is a platform admin."""
    if not is_platform_admin(user):
        msg = "Platform-admin (default-tenant superuser) permission required"
        raise PlatformPermissionError(msg)


# First-superuser claim (one-shot, unauthenticated) ---------------------------
def claim_code(schema_name: str) -> str:
    """Return the claim code that unlocks ``schema_name``'s first-superuser claim.

    Derived from ``SECRET_KEY`` (an HMAC of the schema name), so nothing is stored and
    every process agrees on it, yet only someone with access to the server — its logs
    (``manage.py bootstrap`` prints it), a shell (``manage.py claim_code``), or the
    platform admin's tenant-creation response — can learn it.
    """
    # sha1 explicitly: Django 7 changes the default, which would change every claim code.
    return salted_hmac("uruk.claim_code", schema_name, algorithm="sha1").hexdigest()[:24]


def claim_superuser(email: str, code: str) -> str:
    """Create the active tenant's first superuser and return a set-password link.

    This is the one-shot bootstrap for a tenant with no users yet, reachable over an
    *unauthenticated* endpoint (there is no user to authenticate as). The caller must
    present the tenant's :func:`claim_code`: the link is handed to whoever asks, so
    without that server-side secret anyone who reached an unclaimed tenant first could
    take it over. ``URUK_ADMIN_EMAIL`` additionally restricts the address when set.
    Both failures raise :class:`ClaimNotAllowedError`. Once any superuser exists in the
    schema it refuses permanently (:class:`AlreadyClaimedError`) — after that,
    superusers can only be made by an existing superuser.

    The user is created with an unusable password; the returned relative URL carries a
    single-use, time-limited token (Django's password-reset generator) that
    :func:`set_password_from_token` redeems to set the real password.

    Runs in the currently-active tenant schema (selected by the routing middleware).
    """
    if not constant_time_compare(code, claim_code(current_schema())):
        msg = "Invalid claim code"
        raise ClaimNotAllowedError(msg)
    if settings.URUK_ADMIN_EMAIL and email != settings.URUK_ADMIN_EMAIL:
        msg = "Email is not the configured platform admin address"
        raise ClaimNotAllowedError(msg)
    if User.objects.filter(is_superuser=True).exists():
        msg = "A superuser already exists for this tenant"
        raise AlreadyClaimedError(msg)

    user = User.objects.create_superuser(username=email, email=email)
    user.set_unusable_password()
    user.save(update_fields=["password"])

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return f"/api/set-password?uid={uid}&token={token}"


def set_password_from_token(uid: str, token: str, password: str) -> None:
    """Set a user's password by redeeming a :func:`claim_superuser` link.

    Validates the uid/token pair (single-use, expiring) and the password strength,
    then sets it. Raises :class:`InvalidSetPasswordError` on any failure —
    deliberately without distinguishing bad-uid from bad-token, so the endpoint leaks
    nothing. Runs in the active tenant schema.
    """
    try:
        user = User.objects.get(pk=force_str(urlsafe_base64_decode(uid)))
    except (User.DoesNotExist, ValueError, TypeError) as exc:
        msg = "Invalid set-password link"
        raise InvalidSetPasswordError(msg) from exc

    if not default_token_generator.check_token(user, token):
        msg = "Invalid or expired set-password link"
        raise InvalidSetPasswordError(msg)

    validate_password(password, user)
    user.set_password(password)
    user.save(update_fields=["password"])
