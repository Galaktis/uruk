"""Uruk admin API (django-ninja).

Sync, not async — django-tenants is sync-bound (per-connection schema), so async
views risk tenant-isolation leakage. Endpoints are thin wrappers over
``services.content``; authorization is entirely Django's permission system.
"""

import base64
from uuid import UUID

from django.contrib.auth import authenticate
from django.contrib.auth.models import AbstractBaseUser
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from ninja import NinjaAPI, Status
from ninja.errors import HttpError
from ninja.security import HttpBearer

from api import schemas
from caching.service import render_page_cached
from common.users import authenticated_user, request_user
from content.models import TOKEN_PREFIX, Element, Page, Revision, Setting
from services import auth as auth_svc
from services import content as svc
from services import jwt as jwt_svc
from services import publishing as publishing_svc
from services import tenants as tenants_svc
from tenants.models import Tenant


class TokenAuth(HttpBearer):
    """Authenticate a ``uruk_`` API token — the machine/MCP surface.

    The tenant schema is already selected by the routing middleware, so the token is
    resolved in that schema — a token issued for another tenant will not be found.
    """

    def authenticate(self, request: HttpRequest, token: str) -> AbstractBaseUser | None:
        """Return the tenant user the token authenticates as, or ``None`` (→ next backend)."""
        if not token.startswith(TOKEN_PREFIX):
            return None  # not an API key — let the JWT backend try it
        user = auth_svc.resolve_token(token)
        if user is not None:
            request.user = user
        return user


class JWTAuth(HttpBearer):
    """Authenticate a JWT access token — the interactive/human surface.

    Shares the ``Authorization: Bearer`` header with :class:`TokenAuth`; the two are
    told apart by the ``uruk_`` prefix (API keys) vs a JWT's structure. The user is
    resolved in the token's own schema, so a JWT cannot cross tenants.
    """

    def authenticate(self, request: HttpRequest, token: str) -> AbstractBaseUser | None:
        """Return the user the access token authenticates as, or ``None`` (→ 401)."""
        if token.startswith(TOKEN_PREFIX):
            return None  # an API key — TokenAuth's job
        user = jwt_svc.verify_access(token)
        if user is not None:
            request.user = user
        return user


def session_auth(request: HttpRequest) -> AbstractBaseUser | None:
    """Accept an authenticated Django session (e.g. the admin browser); else ``None``."""
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return user
    return None


def ensure_perm(request: HttpRequest, perm: str) -> None:
    """Enforce a Django model permission, raising 403 if the user lacks it."""
    if not request_user(request).has_perm(perm):
        msg = f"Missing permission: {perm}"
        raise HttpError(403, msg)


# Ninja tries each backend in order; the first to return a user wins. API tokens are
# the machine/MCP surface, JWTs the interactive/human one (both over Bearer), and the
# session backend keeps admin browser use working.
api = NinjaAPI(auth=[TokenAuth(), JWTAuth(), session_auth], title="Uruk Admin API")


# Bootstrap / auth ------------------------------------------------------------
# These are unauthenticated: /bootstrap and /set-password run before any user
# exists, so they cannot require one. Both are guarded inside the service layer.
@api.post("/bootstrap", auth=None, response={200: schemas.ClaimOut})
def bootstrap_superuser(request: HttpRequest, payload: schemas.ClaimIn) -> schemas.ClaimOut:
    """Claim the active tenant's first superuser; returns a one-time set-password link.

    403 if a superuser already exists, if the claim code is wrong, or if
    ``URUK_ADMIN_EMAIL`` is set and the email differs.
    """
    try:
        url = auth_svc.claim_superuser(payload.email, payload.code)
    except auth_svc.AlreadyClaimedError as exc:
        raise HttpError(403, "This tenant has already been claimed") from exc
    except auth_svc.ClaimNotAllowedError as exc:
        raise HttpError(403, str(exc)) from exc
    return schemas.ClaimOut(set_password_url=url)


@api.post("/set-password", auth=None, response={200: None})
def set_password(request: HttpRequest, payload: schemas.SetPasswordIn) -> Status:
    """Redeem a set-password link, setting the user's password. 400 if invalid."""
    try:
        auth_svc.set_password_from_token(payload.uid, payload.token, payload.password)
    except auth_svc.InvalidSetPasswordError as exc:
        raise HttpError(400, str(exc)) from exc
    except ValidationError as exc:
        raise HttpError(400, "; ".join(exc.messages)) from exc
    return Status(200, None)


@api.post("/auth/login", auth=None, response={200: schemas.TokenPairOut})
def login(request: HttpRequest, payload: schemas.LoginIn) -> schemas.TokenPairOut:
    """Exchange username/password for a JWT access + refresh pair. 401 on bad credentials.

    Authenticates against the active tenant's users, so credentials are tenant-scoped.
    """
    user = authenticate(request, username=payload.username, password=payload.password)
    if user is None:
        raise HttpError(401, "Invalid credentials")
    access, refresh = jwt_svc.issue_pair(user)
    return schemas.TokenPairOut(access=access, refresh=refresh)


@api.post("/auth/refresh", auth=None, response={200: schemas.TokenPairOut})
def refresh_tokens(request: HttpRequest, payload: schemas.RefreshIn) -> schemas.TokenPairOut:
    """Exchange a valid refresh token for a new access + refresh pair. 401 if invalid."""
    try:
        access, refresh = jwt_svc.rotate_refresh(payload.refresh)
    except jwt_svc.InvalidTokenError as exc:
        raise HttpError(401, "Invalid or expired refresh token") from exc
    return schemas.TokenPairOut(access=access, refresh=refresh)


# Tenants (platform-admin only) -----------------------------------------------
@api.post("/tenants", response={201: schemas.TenantOut})
def create_tenant(request: HttpRequest, payload: schemas.TenantIn) -> Status:
    """Create a new tenant + primary domain. Requires the default-tenant superuser (403)."""
    try:
        tenant = tenants_svc.create_tenant(request_user(request), schema=payload.schema_name, name=payload.name, domain=payload.domain)
    except auth_svc.PlatformPermissionError as exc:
        raise HttpError(403, str(exc)) from exc
    return Status(201, tenant)


@api.delete("/tenants/{schema_name}", response={204: None})
def delete_tenant(request: HttpRequest, schema_name: str) -> Status:
    """Delete a tenant and drop its schema. Requires the default-tenant superuser (403).

    400 for the default tenant (protected — it is the catch-all and the platform
    admin's own tenant), 404 for an unknown schema.
    """
    try:
        tenants_svc.delete_tenant(request_user(request), schema=schema_name)
    except auth_svc.PlatformPermissionError as exc:
        raise HttpError(403, str(exc)) from exc
    except tenants_svc.DefaultTenantProtectedError as exc:
        raise HttpError(400, str(exc)) from exc
    except Tenant.DoesNotExist as exc:
        raise HttpError(404, f"No tenant with schema {schema_name!r}") from exc
    return Status(204, None)


# Pages -----------------------------------------------------------------------
@api.get("/pages", response=list[schemas.PageOut])
def list_pages(request: HttpRequest) -> list[Page]:
    """List all pages."""
    ensure_perm(request, "content.view_page")
    return svc.list_pages()


@api.post("/pages", response={201: schemas.PageOut})
def create_page(request: HttpRequest, payload: schemas.PageIn) -> Status:
    """Create a page."""
    ensure_perm(request, "content.add_page")
    parent = get_object_or_404(Page, pk=payload.parent_id) if payload.parent_id else None
    root = get_object_or_404(Element, pk=payload.root_element_id) if payload.root_element_id else None
    page = svc.create_page(
        payload.slug,
        payload.name,
        parent=parent,
        root_element=root,
        parameters=payload.parameters,
        position=payload.position,
    )
    return Status(201, page)


@api.get("/pages/{page_id}", response=schemas.PageOut)
def get_page(request: HttpRequest, page_id: UUID) -> Page:
    """Fetch a single page."""
    ensure_perm(request, "content.view_page")
    return get_object_or_404(Page, pk=page_id)


@api.patch("/pages/{page_id}", response=schemas.PageOut)
def update_page(request: HttpRequest, page_id: UUID, payload: schemas.PageUpdate) -> Page:
    """Update a page."""
    ensure_perm(request, "content.change_page")
    page = get_object_or_404(Page, pk=page_id)
    return svc.update_page(page, slug=payload.slug, name=payload.name, parameters=payload.parameters)


@api.delete("/pages/{page_id}", response={204: None})
def delete_page(request: HttpRequest, page_id: UUID) -> Status:
    """Delete a page."""
    ensure_perm(request, "content.delete_page")
    svc.delete_page(get_object_or_404(Page, pk=page_id))
    return Status(204, None)


@api.get("/pages/{page_id}/render")
def render_page_html(request: HttpRequest, page_id: UUID) -> HttpResponse:
    """Render a page's working copy to HTML (cached) — the draft preview that closes the edit loop (§11, §16)."""
    ensure_perm(request, "content.view_page")
    page = get_object_or_404(Page, pk=page_id)
    return HttpResponse(render_page_cached(page), content_type="text/html")


# Elements --------------------------------------------------------------------
@api.post("/elements", response={201: schemas.ElementOut})
def create_element(request: HttpRequest, payload: schemas.ElementIn) -> Status:
    """Create an element."""
    ensure_perm(request, "content.add_element")
    parent = get_object_or_404(Element, pk=payload.parent_id) if payload.parent_id else None
    element = svc.create_element(
        payload.kind,
        parent=parent,
        position=payload.position,
        content=payload.content,
        parameters=payload.parameters,
        is_reusable=payload.is_reusable,
    )
    return Status(201, element)


@api.get("/elements/{element_id}", response=schemas.ElementOut)
def get_element(request: HttpRequest, element_id: UUID) -> Element:
    """Fetch a single element."""
    ensure_perm(request, "content.view_element")
    return get_object_or_404(Element, pk=element_id)


@api.patch("/elements/{element_id}", response=schemas.ElementOut)
def update_element(request: HttpRequest, element_id: UUID, payload: schemas.ElementUpdate) -> Element:
    """Update an element."""
    ensure_perm(request, "content.change_element")
    element = get_object_or_404(Element, pk=element_id)
    return svc.update_element(
        element,
        kind=payload.kind,
        content=payload.content,
        parameters=payload.parameters,
        is_reusable=payload.is_reusable,
    )


@api.delete("/elements/{element_id}", response={204: None})
def delete_element(request: HttpRequest, element_id: UUID) -> Status:
    """Delete an element (and its subtree)."""
    ensure_perm(request, "content.delete_element")
    svc.delete_element(get_object_or_404(Element, pk=element_id))
    return Status(204, None)


# Settings --------------------------------------------------------------------
@api.get("/settings")
def get_settings(request: HttpRequest) -> dict[str, object]:
    """Return all settings as a key -> value mapping."""
    ensure_perm(request, "content.view_setting")
    return svc.get_settings()


@api.put("/settings/{key}", response=schemas.SettingOut)
def set_setting(request: HttpRequest, key: str, payload: schemas.SettingIn) -> Setting:
    """Create or update a setting."""
    ensure_perm(request, "content.change_setting")
    return svc.set_setting(key, payload.value)


# Assets ----------------------------------------------------------------------
@api.post("/assets", response={201: schemas.AssetOut})
def upload_asset(request: HttpRequest, payload: schemas.AssetIn) -> Status:
    """Upload an asset (base64 bytes); its sha256 is computed on save."""
    ensure_perm(request, "content.add_asset")
    data = base64.b64decode(payload.data_base64)
    asset = svc.upload_asset(payload.name, payload.content_type, data)
    return Status(201, asset)


# Publishing (§16) -------------------------------------------------------------
@api.get("/publishing")
def publishing_status(request: HttpRequest) -> publishing_svc.PublishingStatus:
    """Mode (live/draft), the live revision, and what the working copy changes vs it."""
    ensure_perm(request, "content.view_revision")
    return publishing_svc.status()


@api.post("/publishing/publish", response={201: schemas.RevisionOut})
def publish(request: HttpRequest, payload: schemas.PublishIn) -> Status:
    """Publish the working copy as the new live revision."""
    ensure_perm(request, "content.add_revision")
    revision = publishing_svc.publish(message=payload.message, author=authenticated_user(request))
    return Status(201, publishing_svc.revision_summary(revision))


@api.post("/publishing/discard")
def discard_draft(request: HttpRequest, payload: schemas.DiscardIn) -> publishing_svc.PublishingStatus:
    """Throw away unpublished changes: reset the working copy to a revision (default: live)."""
    ensure_perm(request, "content.discard_draft")
    revision = get_object_or_404(Revision, pk=payload.revision_id) if payload.revision_id else None
    publishing_svc.discard_draft(revision)
    return publishing_svc.status()


@api.get("/revisions", response=list[schemas.RevisionOut])
def list_revisions(request: HttpRequest) -> list[publishing_svc.RevisionSummary]:
    """List the publish history, newest first."""
    ensure_perm(request, "content.view_revision")
    return [publishing_svc.revision_summary(revision) for revision in publishing_svc.list_revisions()]


@api.post("/revisions/{revision_id}/rollback", response={201: schemas.RevisionOut})
def rollback(request: HttpRequest, revision_id: UUID) -> Status:
    """Make an older revision live again (published as a new revision; the draft is untouched)."""
    ensure_perm(request, "content.add_revision")
    revision = publishing_svc.rollback(get_object_or_404(Revision, pk=revision_id), author=authenticated_user(request))
    return Status(201, publishing_svc.revision_summary(revision))
