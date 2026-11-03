"""Draft / publish workflow and revision history (ARCHITECTURE.md §16).

The Element/Page/Setting rows are the **working copy**: every API, MCP, and admin
edit lands there. :func:`publish` freezes the working copy into an immutable
:class:`~content.models.Revision`, and the public site serves the latest revision —
so an agent (or a human) can change anything without visitors seeing a half-done
site, review the result, then publish it in one step.

A site that has never been published is in **live mode**: there is no revision to
serve, so the working copy is served directly. The first
publish switches the site to **draft mode** for good.

History is append-only: rolling back republishes an old snapshot as a new revision,
and discarding the draft rewrites the working copy from a snapshot.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, TypedDict

from django.db import transaction

from common.tenancy import current_schema
from content.models import Element, Page, Revision, Setting
from rendering.sources import SnapshotSource
from services.portability import restore_rows, serialize_content

if TYPE_CHECKING:
    from uuid import UUID

    from django.contrib.auth.models import User

#: Live revisions whose parsed snapshot stays in memory, per process. Revisions are
#: immutable and their UUIDv7 ids never reused, so entries never go stale.
SNAPSHOT_CACHE_SIZE = 32

#: The diffable collections of a snapshot, and the field that labels a record in a diff.
_LABELS = {"elements": "kind", "pages": "slug", "settings": "key"}


def live_revision() -> Revision | None:
    """Return the revision the public site currently serves, or ``None`` in live mode."""
    return Revision.objects.order_by("-number").first()


def live_revision_id() -> UUID | None:
    """Return just the live revision's id (one indexed lookup per public request)."""
    return Revision.objects.order_by("-number").values_list("pk", flat=True).first()


def list_revisions() -> list[Revision]:
    """Return the publish history, newest first."""
    return list(Revision.objects.defer("data").select_related("author"))


def publish(*, message: str = "", author: User | None = None) -> Revision:
    """Snapshot the working copy as the new live revision."""
    return _create_revision(serialize_content(), message=message, author=author)


def rollback(revision: Revision, *, author: User | None = None) -> Revision:
    """Make an older revision's content live again, as a new revision (history stays append-only).

    Only the public site changes; the working copy is left as it is. To restart
    editing from that content too, follow up with :func:`discard_draft`.
    """
    return _create_revision(revision.data, message=f"Rollback to #{revision.number}", author=author)


def discard_draft(revision: Revision | None = None) -> None:
    """Replace the working copy with a revision's content (default: the live one).

    Throws away every unpublished change. Ids are preserved, so pages, elements, and
    references keep pointing at the same things. No-op in live mode with no revision.
    """
    revision = revision or live_revision()
    if revision is None:
        return
    with transaction.atomic():
        # Pages first: deleting elements would otherwise null their root_element FK
        # row by row. Cascades take care of child pages/elements and dependencies.
        Page.objects.all().delete()
        Element.objects.all().delete()
        Setting.objects.all().delete()
        restore_rows(revision.data)


class RevisionSummary(TypedDict):
    """A revision's JSON-safe metadata (without the snapshot itself)."""

    id: str
    number: int
    message: str
    author: str | None
    published_at: str


class PublishingStatus(TypedDict):
    """The publishing state: mode, live revision, and the unpublished diff."""

    mode: str
    live_revision: RevisionSummary | None
    has_unpublished_changes: bool
    changes: dict[str, dict[str, list[dict[str, str]]]]


def diff(base: dict | None, target: dict) -> dict[str, dict[str, list[dict[str, str]]]]:
    """Compare two snapshots record by record: what ``target`` adds, removes, and changes.

    ``base=None`` (never published) reports everything in ``target`` as added. Each
    entry is ``{"id", "label"}`` — the label (an element's kind, a page's slug, a
    setting's key) keeps the diff readable for a human or an agent reviewing it.
    """
    result: dict[str, dict[str, list[dict[str, str]]]] = {}
    for collection, label_field in _LABELS.items():
        before = {record["id"]: record for record in (base or {}).get(collection, [])}
        after = {record["id"]: record for record in target[collection]}

        def entry(record: dict, label_field: str = label_field) -> dict[str, str]:
            return {"id": record["id"], "label": str(record[label_field])}

        result[collection] = {
            "added": [entry(after[i]) for i in after.keys() - before.keys()],
            "removed": [entry(before[i]) for i in before.keys() - after.keys()],
            "changed": [entry(after[i]) for i in after.keys() & before.keys() if after[i] != before[i]],
        }
    return result


def status() -> PublishingStatus:
    """Summarize the publishing state: mode, live revision, and the unpublished diff."""
    revision = live_revision()
    changes = diff(revision.data if revision else None, serialize_content())
    return {
        "mode": "draft" if revision else "live",
        "live_revision": revision_summary(revision) if revision else None,
        "has_unpublished_changes": any(entries for collection in changes.values() for entries in collection.values()),
        "changes": changes,
    }


def revision_summary(revision: Revision) -> RevisionSummary:
    """Return a revision's JSON-safe metadata (without the snapshot itself)."""
    return {
        "id": str(revision.pk),
        "number": revision.number,
        "message": revision.message,
        "author": revision.author.get_username() if revision.author else None,
        "published_at": revision.published_at.isoformat(),
    }


def snapshot_source(revision_id: UUID) -> SnapshotSource:
    """Return the (process-cached) render source for a revision of the current tenant."""
    return _snapshot_source(current_schema(), revision_id)


@lru_cache(maxsize=SNAPSHOT_CACHE_SIZE)
def _snapshot_source(schema_name: str, revision_id: UUID) -> SnapshotSource:
    """Load and index a revision's snapshot; keyed by schema too, so tenants never share entries."""
    del schema_name  # part of the cache key only: the query runs in the active schema
    return SnapshotSource(Revision.objects.get(pk=revision_id).data)


def _create_revision(data: dict, *, message: str, author: User | None) -> Revision:
    """Append a revision with the next number (row-locked, so concurrent publishes serialize)."""
    with transaction.atomic():
        latest = Revision.objects.select_for_update().order_by("-number").only("number").first()
        number = latest.number + 1 if latest else 1
        return Revision.objects.create(number=number, message=message, author=author, data=data)
