"""JWT issuance and verification for interactive (human) API clients.

Access + refresh tokens signed HS256 with ``SECRET_KEY`` — no separate key material.
This is the auth surface for browsers/humans; **MCP and other machine clients keep the
long-lived** :class:`~content.models.ApiToken` (a headless process has nobody to run a
refresh dance, so a rotating token buys nothing there).

Every token carries the tenant ``schema`` it was minted in, and verification resolves
the user *inside that schema*. A token minted for one tenant therefore cannot
authenticate against another — the same isolation the API tokens have.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import jwt
from django.conf import settings
from django.contrib.auth.models import User
from django_tenants.utils import schema_context

from common.tenancy import current_schema

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

ALGORITHM = "HS256"
ACCESS = "access"
REFRESH = "refresh"


class InvalidTokenError(Exception):
    """Raised when a JWT is malformed, expired, of the wrong type, or names no user."""


def _encode(user: AbstractBaseUser, *, token_type: str, ttl: int) -> str:
    """Sign a token of ``token_type`` for ``user`` in the active schema, expiring in ``ttl`` s."""
    now = datetime.now(tz=UTC)
    payload = {
        "uid": str(user.pk),
        "schema": current_schema(),
        "type": token_type,
        "exp": now + timedelta(seconds=ttl),
        "iat": now,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def issue_pair(user: AbstractBaseUser) -> tuple[str, str]:
    """Return a fresh ``(access, refresh)`` pair for ``user`` in the active tenant schema."""
    return (
        _encode(user, token_type=ACCESS, ttl=settings.URUK_JWT_ACCESS_TTL),
        _encode(user, token_type=REFRESH, ttl=settings.URUK_JWT_REFRESH_TTL),
    )


def _decode(token: str, *, expected_type: str) -> dict:
    """Decode and validate a token's signature, expiry, and type; return its claims."""
    try:
        claims = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError as exc:
        msg = "Invalid or expired token"
        raise InvalidTokenError(msg) from exc
    if claims.get("type") != expected_type:
        msg = f"Expected a {expected_type} token"
        raise InvalidTokenError(msg)
    return claims


def verify_access(token: str) -> User | None:
    """Return the user an access token authenticates as, or ``None`` if it is invalid.

    Returning ``None`` (rather than raising) suits the Ninja auth backend, which treats
    a ``None`` result as "not authenticated" (→ 401). The user is loaded in the token's
    own schema, so a token cannot cross tenants.
    """
    try:
        claims = _decode(token, expected_type=ACCESS)
    except InvalidTokenError:
        return None
    with schema_context(claims["schema"]):
        return User.objects.filter(pk=claims["uid"]).first()


def rotate_refresh(refresh_token: str) -> tuple[str, str]:
    """Validate a refresh token and issue a new ``(access, refresh)`` pair.

    Raises :class:`InvalidTokenError` if the refresh token is invalid, expired, or the
    user no longer exists. Rotation runs in the token's own schema.
    """
    claims = _decode(refresh_token, expected_type=REFRESH)
    with schema_context(claims["schema"]):
        user = User.objects.filter(pk=claims["uid"]).first()
        if user is None:
            msg = "Token names no existing user"
            raise InvalidTokenError(msg)
        return issue_pair(user)
