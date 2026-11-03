"""Cache key helpers, including the tenant-isolating KEY_FUNCTION."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from common.tenancy import current_schema

if TYPE_CHECKING:
    from uuid import UUID


def tenant_key_function(key: str, key_prefix: str, version: int) -> str:
    """Prefix every cache key with the active tenant schema, isolating cached output."""
    return f"{current_schema()}:{key_prefix}:{version}:{key}"


def page_cache_key(page_pk: UUID) -> str:
    """Cache key for a page's full computed HTML output."""
    return f"page:{page_pk}"


def fragment_cache_key(element_pk: UUID) -> str:
    """Cache key for an element subtree's HTML (htmx fragment)."""
    return f"fragment:{element_pk}"


def published_page_cache_key(revision_pk: UUID, path: str) -> str:
    """Cache key for a published page's full HTML document, by URL path (§16).

    A revision is immutable, so this entry never needs invalidating: publishing a new
    revision simply moves serving to new keys. The path is hashed to bound key length.
    """
    return f"published:{revision_pk}:page:{hashlib.sha256(path.encode()).hexdigest()[:32]}"


def published_fragment_cache_key(revision_pk: UUID, element_pk: UUID) -> str:
    """Cache key for a published element subtree's HTML (htmx fragment, §16)."""
    return f"published:{revision_pk}:fragment:{element_pk}"
