"""Django admin registrations for the tenant-schema CMS models.

The admin is a per-tenant inspection/debugging surface, not the primary editing
UI — content operations go through ``services`` via the API and MCP server
(ARCHITECTURE.md §11). Edits made here go through the ORM like any other write,
so element saves/deletes trigger the same cache-invalidation signals (§7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import admin

from content.models import ApiToken, Asset, Element, Page, PageDependency, Revision, Setting

if TYPE_CHECKING:
    from django.http import HttpRequest


@admin.register(Element)
class ElementAdmin(admin.ModelAdmin):
    """Browse and edit the element composition trees."""

    list_display = ("__str__", "kind", "parent", "position", "is_reusable", "updated")
    list_filter = ("kind", "is_reusable")
    search_fields = ("kind", "content")
    autocomplete_fields = ("parent",)


@admin.register(Page)
class PageAdmin(admin.ModelAdmin):
    """Browse and edit the site's URL tree."""

    list_display = ("slug", "name", "parent", "position", "root_element", "updated")
    search_fields = ("slug", "name")
    autocomplete_fields = ("parent", "root_element")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    """Inspect stored assets.

    The bytes themselves are uploaded via the API/MCP, so the (non-editable)
    ``data`` column is absent from the form; ``sha256`` is recomputed on save
    whenever inline bytes are present.
    """

    list_display = ("name", "content_type", "sha256", "storage_key", "updated")
    search_fields = ("name", "sha256")


@admin.register(Setting)
class SettingAdmin(admin.ModelAdmin):
    """Browse and edit the namespaced DB-stored configuration entries."""

    list_display = ("key", "updated")
    search_fields = ("key",)


@admin.register(Revision)
class RevisionAdmin(admin.ModelAdmin):
    """Read-only publish history (§16).

    Revisions are immutable snapshots created by publishing through the API/MCP;
    editing one would rewrite what the public site serves behind the history's back.
    """

    list_display = ("number", "message", "author", "published_at")
    exclude = ("data",)

    def has_add_permission(self, request: HttpRequest) -> bool:  # noqa: ARG002 - Django's fixed permission-hook signature
        """Forbid adding: revisions are only ever created by publishing."""
        return False

    def has_change_permission(self, request: HttpRequest, obj: Revision | None = None) -> bool:  # noqa: ARG002 - Django's fixed permission-hook signature
        """Forbid editing: a published snapshot is immutable."""
        return False


@admin.register(PageDependency)
class PageDependencyAdmin(admin.ModelAdmin):
    """Read-only view of the page→element dependency index (§7).

    These rows are machine-managed by the caching layer — written when a page is
    rendered, consumed by invalidation. Hand-editing them would silently break
    cache eviction, so the admin only allows looking.
    """

    list_display = ("page", "element_id")
    list_select_related = ("page",)

    def has_add_permission(self, request: HttpRequest) -> bool:  # noqa: ARG002 - Django's fixed permission-hook signature
        """Forbid adding: dependency rows are only ever written by the caching layer."""
        return False

    def has_change_permission(self, request: HttpRequest, obj: PageDependency | None = None) -> bool:  # noqa: ARG002 - Django's fixed permission-hook signature
        """Forbid editing: dependency rows are only ever rewritten by the caching layer."""
        return False

    def has_delete_permission(self, request: HttpRequest, obj: PageDependency | None = None) -> bool:  # noqa: ARG002 - Django's fixed permission-hook signature
        """Forbid deleting: a missing row would stop the page being evicted on element changes."""
        return False


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin):
    """Inspect and revoke (delete) API tokens.

    Creating tokens here is disabled: only the sha256 hash is stored, and the
    plaintext must be surfaced exactly once at generation — use
    ``manage.py create_api_token`` (or :meth:`content.models.ApiToken.generate`).
    """

    list_display = ("name", "user", "last_used", "created")
    readonly_fields = ("last_used",)
    autocomplete_fields = ("user",)

    def has_add_permission(self, request: HttpRequest) -> bool:  # noqa: ARG002 - Django's fixed permission-hook signature
        """Forbid adding: tokens must be minted via ``ApiToken.generate`` so the plaintext is shown once."""
        return False
