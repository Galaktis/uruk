"""Asset byte storage — Postgres by default, with env-gated escape hatches.

By default asset bytes live in the ``Asset.data`` column (``bytea``) in the tenant
schema, and no backend here is used at all. When ``URUK_STORAGE_BACKEND`` names a
backend, the bytes move out — filesystem or an S3-compatible bucket — while the
``Asset`` row (hash, name, content-type, ``storage_key``) stays in the schema
(ARCHITECTURE.md §9).

Blobs are **content-addressed by their sha256**: identical content dedups for free
and the key is stable, mirroring the immutable-URL serving design. A backend only
needs to move bytes in and out by key; it knows nothing about tenants or assets.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings

if TYPE_CHECKING:
    from collections.abc import Mapping


class StorageBackend(ABC):
    """Stores and retrieves opaque byte blobs keyed by a content hash."""

    @abstractmethod
    def write(self, key: str, data: bytes, content_type: str) -> None:
        """Persist ``data`` under ``key`` (idempotent — same key means same bytes)."""

    @abstractmethod
    def read(self, key: str) -> bytes:
        """Return the bytes stored under ``key``."""


class FileSystemBackend(StorageBackend):
    """Stores blobs as files under a root directory, one file per content hash."""

    def __init__(self, root: str) -> None:
        """Create the backend rooted at ``root`` (created on first write)."""
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        """Map a content hash to its on-disk path, sharded by the first two chars."""
        return self.root / key[:2] / key

    def write(self, key: str, data: bytes, content_type: str) -> None:  # noqa: ARG002 - content_type is part of the interface; filesystem does not use it
        """Write the blob, creating parent directories as needed."""
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def read(self, key: str) -> bytes:
        """Read the blob back from disk."""
        return self._path(key).read_bytes()


class BotoStorageBackend(StorageBackend):
    """Stores blobs in an S3-compatible bucket via boto3 (imported lazily)."""

    def __init__(self, bucket: str, *, prefix: str = "", endpoint_url: str = "", region: str = "") -> None:
        """Create the backend for ``bucket``; boto3 is imported only here."""
        import boto3  # noqa: PLC0415 - lazy: boto3 is only a dependency when this backend is selected  # ty: ignore[import-not-found]

        self.bucket = bucket
        self.prefix = prefix
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region or None,
        )

    def _key(self, key: str) -> str:
        """Prepend the configured prefix to a content hash."""
        return f"{self.prefix}{key}"

    def write(self, key: str, data: bytes, content_type: str) -> None:
        """Upload the blob to the bucket."""
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data, ContentType=content_type)

    def read(self, key: str) -> bytes:
        """Download the blob from the bucket."""
        response = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        return response["Body"].read()


def get_storage_backend() -> StorageBackend | None:
    """Return the configured storage backend, or ``None`` to keep bytes in Postgres.

    Selected by ``URUK_STORAGE_BACKEND``: unset (or empty) keeps bytes in
    ``Asset.data``; ``"filesystem"`` and ``"boto"`` move them out.
    """
    name = settings.URUK_STORAGE_BACKEND
    if not name:
        return None
    if name == "filesystem":
        if not settings.URUK_STORAGE_ROOT:
            msg = "URUK_STORAGE_BACKEND=filesystem requires URUK_STORAGE_ROOT"
            raise ValueError(msg)
        return FileSystemBackend(settings.URUK_STORAGE_ROOT)
    if name == "boto":
        if not settings.URUK_STORAGE_BUCKET:
            msg = "URUK_STORAGE_BACKEND=boto requires URUK_STORAGE_BUCKET"
            raise ValueError(msg)
        return BotoStorageBackend(
            settings.URUK_STORAGE_BUCKET,
            prefix=settings.URUK_STORAGE_PREFIX,
            endpoint_url=settings.URUK_STORAGE_ENDPOINT_URL,
            region=settings.URUK_STORAGE_REGION,
        )
    msg = f"Unknown URUK_STORAGE_BACKEND: {name!r} (expected 'filesystem' or 'boto')"
    raise ValueError(msg)


# A tiny indirection so callers can be tested/overridden without touching env vars.
_KNOWN_BACKENDS: Mapping[str, type[StorageBackend]] = {
    "filesystem": FileSystemBackend,
    "boto": BotoStorageBackend,
}
