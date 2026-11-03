"""Pydantic schemas for the admin API (ARCHITECTURE.md §11)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from ninja import Schema

from services import auth as auth_svc

if TYPE_CHECKING:
    from tenants.models import Tenant

JSONValue = dict | list | str | int | float | bool | None


class PageIn(Schema):
    """Payload for creating a page."""

    slug: str
    name: str
    parameters: dict = {}
    position: int = 0
    parent_id: UUID | None = None
    root_element_id: UUID | None = None


class PageUpdate(Schema):
    """Partial update for a page (omitted/null fields are left unchanged)."""

    slug: str | None = None
    name: str | None = None
    parameters: dict | None = None


class PageOut(Schema):
    """Serialized page."""

    id: UUID
    slug: str
    name: str
    parameters: dict
    position: int
    parent_id: UUID | None = None
    root_element_id: UUID | None = None


class ElementIn(Schema):
    """Payload for creating an element."""

    kind: str
    parent_id: UUID | None = None
    position: int = 0
    content: str = ""
    parameters: dict = {}
    is_reusable: bool = False


class ElementUpdate(Schema):
    """Partial update for an element (omitted/null fields are left unchanged)."""

    kind: str | None = None
    content: str | None = None
    parameters: dict | None = None
    is_reusable: bool | None = None


class ElementOut(Schema):
    """Serialized element."""

    id: UUID
    kind: str
    parent_id: UUID | None = None
    position: int
    content: str
    parameters: dict
    is_reusable: bool


class SettingIn(Schema):
    """Payload for setting a configuration value."""

    value: JSONValue


class SettingOut(Schema):
    """Serialized setting."""

    key: str
    value: JSONValue


class AssetIn(Schema):
    """Payload for uploading an asset (base64-encoded bytes)."""

    name: str
    content_type: str
    data_base64: str


class AssetOut(Schema):
    """Serialized asset (no payload bytes)."""

    id: UUID
    name: str
    content_type: str
    sha256: str


# Auth / bootstrap ------------------------------------------------------------
class ClaimIn(Schema):
    """Payload to claim a tenant's first superuser (``code``: see ``manage.py claim_code``)."""

    email: str
    code: str


class ClaimOut(Schema):
    """Response carrying the one-time set-password link."""

    set_password_url: str


class SetPasswordIn(Schema):
    """Payload to redeem a set-password link."""

    uid: str
    token: str
    password: str


class LoginIn(Schema):
    """Payload for a username/password login."""

    username: str
    password: str


class RefreshIn(Schema):
    """Payload carrying a refresh token."""

    refresh: str


class TokenPairOut(Schema):
    """A freshly-issued JWT access + refresh pair."""

    access: str
    refresh: str


class TenantIn(Schema):
    """Payload to create a new tenant and its primary domain."""

    schema_name: str
    name: str
    domain: str


class TenantOut(Schema):
    """Serialized tenant, with the claim code its first superuser needs."""

    id: UUID
    schema_name: str
    name: str
    claim_code: str

    @staticmethod
    def resolve_claim_code(obj: Tenant) -> str:
        """Derive the tenant's claim code, for the platform admin to hand over."""
        return auth_svc.claim_code(obj.schema_name)


class PublishIn(Schema):
    """Payload for publishing the working copy."""

    message: str = ""


class DiscardIn(Schema):
    """Payload for discarding the draft (default: reset to the live revision)."""

    revision_id: UUID | None = None


class RevisionOut(Schema):
    """A published revision's metadata (the snapshot itself is not returned)."""

    id: UUID
    number: int
    message: str
    author: str | None = None
    published_at: str
