"""Tenant archive/restore (ARCHITECTURE.md §10).

A site archive is essentially a dump of one tenant schema's content rows. Because
every row has a globally-unique UUIDv7 primary key, an archive restores onto another
Uruk instance with zero primary-key collisions — and the restored site keeps the same
ids, so references stay valid. ``PageDependency`` is a cache artefact and is not
archived; it is rebuilt lazily on the next render. Published revisions (§16) are
archived, so a site's history travels with it.
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import TYPE_CHECKING

from django_tenants.utils import tenant_context

from content.models import Asset, Element, Page, Revision, Setting
from services.storage import get_storage_backend
from tenants.models import Tenant

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Version written by :func:`archive_tenant`. ``revisions`` (publish history) is optional.
ARCHIVE_VERSION = 1
#: Versions :func:`restore_content` accepts.
SUPPORTED_ARCHIVE_VERSIONS = {1}


class ArchiveVersionError(ValueError):
    """Raised when an archive's version is not supported by this Uruk."""


# Archive ---------------------------------------------------------------------
def serialize_content() -> dict:
    """Serialize the current tenant's working copy: elements, pages, and settings.

    This is the record format shared by archives and published revisions
    (:mod:`services.publishing`) — everything a render reads except asset bytes,
    which are content-addressed and referenced by id.
    """
    return {
        "elements": [
            {
                "id": str(element.pk),
                "kind": element.kind,
                "parent_id": str(element.parent_id) if element.parent_id else None,
                "position": element.position,
                "content": element.content,
                "parameters": element.parameters,
                "is_reusable": element.is_reusable,
            }
            for element in Element.objects.all()
        ],
        "pages": [
            {
                "id": str(page.pk),
                "slug": page.slug,
                "name": page.name,
                "parent_id": str(page.parent_id) if page.parent_id else None,
                "position": page.position,
                "root_element_id": str(page.root_element_id) if page.root_element_id else None,
                "parameters": page.parameters,
            }
            for page in Page.objects.all()
        ],
        "settings": [{"id": str(s.pk), "key": s.key, "value": s.value} for s in Setting.objects.all()],
    }


def archive_tenant(tenant: Tenant) -> dict:
    """Serialize a tenant's content and publish history into a portable, JSON-safe archive."""
    with tenant_context(tenant):
        return {
            "uruk_archive_version": ARCHIVE_VERSION,
            "tenant": {"name": tenant.name},
            **serialize_content(),
            # Always serialise the bytes themselves (never the storage_key), so an
            # archive is self-contained regardless of the source backend and restores
            # onto an instance with any backend (ARCHITECTURE.md §9).
            "assets": [
                {
                    "id": str(asset.pk),
                    "name": asset.name,
                    "content_type": asset.content_type,
                    "sha256": asset.sha256,
                    "data_base64": base64.b64encode(asset.read_bytes()).decode(),
                }
                for asset in Asset.objects.all()
            ],
            # Authors are not archived: users are per-instance, not site content.
            "revisions": [
                {
                    "id": str(revision.pk),
                    "number": revision.number,
                    "message": revision.message,
                    "published_at": revision.published_at.isoformat(),
                    "data": revision.data,
                }
                for revision in Revision.objects.order_by("number")
            ],
        }


# Restore ---------------------------------------------------------------------
def restore_tenant(archive: dict, *, schema_name: str, name: str | None = None) -> Tenant:
    """Restore an archive into a brand-new tenant schema, preserving every id.

    The archive's publish history comes along. An archive without any (a site that
    was never published, or a hand-built archive) is published once on arrival, so the restored
    site starts in draft mode with exactly the archived content live (§16).
    """
    from services.publishing import publish  # noqa: PLC0415 - publishing builds on this module

    _check_version(archive)
    tenant = Tenant.objects.create(
        schema_name=schema_name,
        name=name or archive["tenant"]["name"],
    )
    with tenant_context(tenant):
        restore_content(archive)
        if not archive.get("revisions"):
            publish(message="Restored from archive")
    return tenant


def restore_content(archive: dict) -> None:
    """Insert an archive's content rows into the current (empty) tenant schema.

    The write side of :func:`archive_tenant`, factored out so it also serves
    first-boot seeding (:mod:`tenants.default`), where the tenant already exists.
    Ids are preserved verbatim; callers are responsible for running inside a
    tenant context whose content tables are empty.
    """
    _check_version(archive)
    restore_rows(archive)
    _restore_assets(archive["assets"])
    Revision.objects.bulk_create(
        Revision(
            id=record["id"],
            number=record["number"],
            message=record["message"],
            published_at=datetime.fromisoformat(record["published_at"]),
            data=record["data"],
        )
        for record in archive.get("revisions", [])
    )


def restore_rows(records: dict) -> None:
    """Insert elements, pages, and settings (the :func:`serialize_content` format), ids preserved."""
    Element.objects.bulk_create(
        Element(
            id=record["id"],
            kind=record["kind"],
            parent_id=record["parent_id"],
            position=record["position"],
            content=record["content"],
            parameters=record["parameters"],
            is_reusable=record["is_reusable"],
        )
        for record in _parents_first(records["elements"])
    )
    Page.objects.bulk_create(
        Page(
            id=record["id"],
            slug=record["slug"],
            name=record["name"],
            parent_id=record["parent_id"],
            position=record["position"],
            root_element_id=record["root_element_id"],
            parameters=record["parameters"],
        )
        for record in _parents_first(records["pages"])
    )
    Setting.objects.bulk_create(Setting(id=record["id"], key=record["key"], value=record["value"]) for record in records["settings"])


def _check_version(archive: dict) -> None:
    """Raise :class:`ArchiveVersionError` unless the archive's version is supported."""
    version = archive.get("uruk_archive_version")
    if version not in SUPPORTED_ARCHIVE_VERSIONS:
        msg = f"Unsupported archive version: {version!r}"
        raise ArchiveVersionError(msg)


def _restore_assets(records: Iterable[dict]) -> None:
    """Restore assets, routing bytes to the destination's active storage backend.

    With no backend the bytes go inline into ``Asset.data``; with one active they are
    written to it under the sha256 and only ``storage_key`` is kept on the row. Either
    way the original id and hash are preserved (ARCHITECTURE.md §9/§10).
    """
    backend = get_storage_backend()
    assets = []
    for record in records:
        data = base64.b64decode(record["data_base64"])
        sha256 = record["sha256"]
        if backend is None:
            assets.append(Asset(id=record["id"], name=record["name"], content_type=record["content_type"], sha256=sha256, data=data))
        else:
            backend.write(sha256, data, record["content_type"])
            assets.append(Asset(id=record["id"], name=record["name"], content_type=record["content_type"], sha256=sha256, data=b"", storage_key=sha256))
    Asset.objects.bulk_create(assets)


def _parents_first(records: Iterable[dict]) -> list[dict]:
    """Order self-nesting records so each parent precedes its children (FK-safe insert)."""
    by_id = {record["id"]: record for record in records}
    ordered: list[dict] = []
    seen: set[str] = set()

    def visit(record: dict) -> None:
        if record["id"] in seen:
            return
        seen.add(record["id"])
        parent_id = record["parent_id"]
        if parent_id and parent_id in by_id:
            visit(by_id[parent_id])
        ordered.append(record)

    for record in by_id.values():
        visit(record)
    return ordered
