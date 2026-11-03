"""Tenant-schema CMS models: the element tree, pages, assets, and settings.

These live in ``TENANT_APPS`` — each tenant gets its own copy of these tables in
its own Postgres schema. A page is composed at runtime from a tree of
:class:`Element` rows (see ARCHITECTURE.md §5/§6); nothing here is committed per
site, only data.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING, Any

from django.db import models
from django.utils import timezone

from common.models import TimestampedModel, UUID7Model
from common.validators import validate_lowercase

if TYPE_CHECKING:
    from uuid import UUID

    from django.db.models.fields.related_descriptors import RelatedManager

#: Prefix on the plaintext token, so a leaked secret is recognisably Uruk's.
TOKEN_PREFIX = "uruk_"  # noqa: S105 - a label, not a secret; the secret is the random suffix


def hash_token(raw: str) -> str:
    """Return the sha256 hex digest a raw token is stored and looked up by."""
    return hashlib.sha256(raw.encode()).hexdigest()


class Element(UUID7Model, TimestampedModel):
    """A node in a page's composition tree.

    ``kind`` selects the renderer; ``parameters`` carries structured data and
    ``content`` free text/HTML. Elements self-nest via ``parent``/``position``,
    and reusable ones (headers, footers) can be shared across pages.
    """

    kind = models.CharField(max_length=128)
    parent = models.ForeignKey(
        "content.Element",
        blank=True,
        null=True,
        on_delete=models.CASCADE,
        related_name="children",
    )
    position = models.PositiveIntegerField(default=0)
    parameters = models.JSONField(default=dict, blank=True)
    content = models.TextField(default="", blank=True)
    is_reusable = models.BooleanField(default=False)

    if TYPE_CHECKING:
        children: RelatedManager[Element]  # reverse of ``parent`` (django-stubs needs its mypy plugin to infer it)

    class Meta:
        ordering = ["position", "created"]

    def __str__(self) -> str:
        return f"Element {self.kind} ({self.pk})"


class Page(UUID7Model, TimestampedModel):
    """A page in the site's URL tree, rendered from its ``root_element``."""

    slug = models.SlugField(max_length=256, validators=[validate_lowercase])
    name = models.CharField(max_length=256)
    parent = models.ForeignKey(
        "content.Page",
        blank=True,
        null=True,
        on_delete=models.CASCADE,
        related_name="children",
    )
    position = models.PositiveIntegerField(default=0)
    root_element = models.ForeignKey(
        "content.Element",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="pages",
    )
    parameters = models.JSONField(default=dict, blank=True)

    if TYPE_CHECKING:
        children: RelatedManager[Page]  # reverse of ``parent``
        _uruk_previous_parent_id: UUID | None  # stashed by caching.signals before a save

    class Meta:
        ordering = ["position", "slug"]
        constraints = [
            models.UniqueConstraint(fields=["parent", "slug"], name="unique_slug_per_parent"),
        ]

    def __str__(self) -> str:
        return f"Page {self.slug}"


class Asset(UUID7Model, TimestampedModel):
    """A binary/text asset (JS, CSS, image), row-in-DB, bytes here or in a backend.

    ``sha256`` is the content hash, used for immutable cache-busting URLs
    (ARCHITECTURE.md §9). The bytes live in ``data`` (Postgres ``bytea``) by default;
    when a storage backend is active they live externally and ``storage_key`` names
    the blob while ``data`` is empty. Read the bytes back via :meth:`read_bytes`, which
    hides which of the two holds them. ``sha256`` is (re)computed from ``data`` on save
    whenever fresh bytes are present.
    """

    name = models.CharField(max_length=256)
    content_type = models.CharField(max_length=128)
    #: Inline bytes when no storage backend is active; empty when they live externally.
    data = models.BinaryField(blank=True, default=b"")
    #: Key of the external blob when a storage backend holds the bytes; empty otherwise.
    storage_key = models.CharField(max_length=256, blank=True, default="")
    sha256 = models.CharField(max_length=64, editable=False, db_index=True)

    def __str__(self) -> str:
        return f"Asset {self.name}"

    def save(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401 - forwards Model.save's arguments unchanged
        """Recompute the content hash from inline bytes when they are present.

        When the bytes live in a backend (``storage_key`` set, ``data`` empty), the
        ``sha256`` was computed at upload time and is preserved as-is.
        """
        if self.data:
            self.sha256 = hashlib.sha256(bytes(self.data)).hexdigest()
        super().save(*args, **kwargs)

    def read_bytes(self) -> bytes:
        """Return the asset's bytes, from the DB column or the storage backend."""
        if self.storage_key:
            from services.storage import get_storage_backend  # noqa: PLC0415 - avoid import cycle content <- services

            backend = get_storage_backend()
            if backend is None:
                msg = f"Asset {self.pk} has a storage_key but no storage backend is configured"
                raise RuntimeError(msg)
            return backend.read(self.storage_key)
        return bytes(self.data)


class Setting(UUID7Model, TimestampedModel):
    """A database-stored configuration entry, keyed by a namespaced string."""

    key = models.CharField(max_length=256, unique=True)
    value = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"Setting {self.key}"


class PageDependency(UUID7Model):
    """Reverse index of which elements a page's cached output depends on (§7).

    ``element_id`` is a bare UUID (not a FK), so the rows survive an element
    deletion long enough for the post_delete signal to find the pages to evict. It can
    also hold a *page* id: a ``page_list`` depends on its collection's parent page, so
    child-page changes evict the listing (UUIDv7 ids never collide across tables).
    """

    page = models.ForeignKey("content.Page", on_delete=models.CASCADE, related_name="dependencies")
    element_id = models.UUIDField(db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["page", "element_id"], name="unique_page_element_dependency"),
        ]

    def __str__(self) -> str:
        return f"PageDependency(page={self.page_id}, element={self.element_id})"


class Revision(UUID7Model, TimestampedModel):
    """An immutable, published snapshot of the site's content (ARCHITECTURE.md §16).

    The Element/Page/Setting rows are the **working copy** that the API, MCP, and admin
    edit. Publishing freezes that working copy into a new Revision; the public site is
    served from the latest one, so edits stay invisible until the next publish. A site
    with no Revision at all is in *live mode*: the working copy is served directly.

    ``data`` holds the elements, pages, and settings in the archive's record format
    (:mod:`services.portability`). Assets are not copied — they are content-addressed
    and never mutated, so a snapshot refers to them by id. Rolling back republishes an
    old snapshot as a *new* Revision: history is append-only.
    """

    number = models.PositiveIntegerField(unique=True)
    message = models.CharField(max_length=512, default="", blank=True)
    author = models.ForeignKey("auth.User", blank=True, null=True, on_delete=models.SET_NULL, related_name="revisions")
    published_at = models.DateTimeField(default=timezone.now)
    data = models.JSONField()

    class Meta:
        ordering = ["-number"]
        permissions = [("discard_draft", "Can discard unpublished changes")]

    def __str__(self) -> str:
        return f"Revision #{self.number}"


class ApiToken(UUID7Model, TimestampedModel):
    """A bearer token authenticating an API/MCP client as a tenant user.

    Tokens live in the tenant schema, so they are isolated per site and travel with
    it. Only the sha256 ``key_hash`` is stored — the plaintext is shown once, at
    creation, and never again. What the token can do is exactly its ``user``'s Django
    permissions; there is no parallel authorization model (ARCHITECTURE.md §11).
    """

    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="api_tokens")
    name = models.CharField(max_length=256, default="", blank=True)
    key_hash = models.CharField(max_length=64, unique=True, editable=False)
    last_used = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"ApiToken({self.name or self.pk} for {self.user_id})"

    @classmethod
    def generate(cls, user: models.Model, *, name: str = "") -> tuple[ApiToken, str]:
        """Create a token for ``user``; return the instance and its one-time plaintext."""
        raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
        token = cls.objects.create(user=user, name=name, key_hash=hash_token(raw))
        return token, raw
