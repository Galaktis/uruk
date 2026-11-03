"""Tenant archive/restore: a site moves to a new schema, ids and output intact (§10)."""

import hashlib
import json

import pytest
from django.test import override_settings
from django_tenants.utils import tenant_context

from caching.service import render_page_cached
from content.models import Asset, Element, Page
from services import content as svc
from services.portability import ArchiveVersionError, archive_tenant, restore_tenant
from tenants.models import Tenant


@pytest.mark.django_db(transaction=True)
def test_archive_restore_roundtrip_preserves_ids_and_output() -> None:
    src = Tenant.objects.create(schema_name="arch_src", name="Source")
    with tenant_context(src):
        root = svc.create_element("container", parameters={"tag": "section"})
        svc.create_element("text", parent=root, position=0, content="Hi")
        page = svc.create_page("index", "Index", root_element=root)
        svc.set_setting("site.title", {"text": "Uruk"})
        svc.upload_asset("main.css", "text/css", b"body{}")

        src_html = str(render_page_cached(Page.objects.get(slug="index")))
        src_page_id = page.pk
        src_element_ids = set(Element.objects.values_list("id", flat=True))

    # The archive must survive a JSON round-trip (cross-instance transfer).
    archive = json.loads(json.dumps(archive_tenant(src)))

    dst = restore_tenant(archive, schema_name="arch_dst", name="Restored")
    assert dst.schema_name != src.schema_name

    with tenant_context(dst):
        # Globally-unique UUIDs are preserved — fearless cross-instance restore.
        assert Page.objects.get(slug="index").pk == src_page_id
        assert set(Element.objects.values_list("id", flat=True)) == src_element_ids

        # Identical rendered output (cache keys are schema-prefixed, so no collision).
        assert str(render_page_cached(Page.objects.get(slug="index"))) == src_html

        # Settings and assets carried over intact.
        assert svc.get_setting("site.title") == {"text": "Uruk"}
        asset = Asset.objects.get(name="main.css")
        assert bytes(asset.data) == b"body{}"
        assert asset.sha256 == hashlib.sha256(b"body{}").hexdigest()


@pytest.mark.django_db(transaction=True)
def test_archive_from_storage_backend_restores_onto_postgres(tmp_path) -> None:  # noqa: ANN001 - pytest tmp_path fixture
    """A site archived from a filesystem-backed instance restores onto a DB-only one.

    The archive always serialises the bytes themselves (never the storage_key), so it
    is self-contained; the destination (no backend) restores them inline (§9).
    """
    src = Tenant.objects.create(schema_name="arch_fs_src", name="FsSource")
    with override_settings(URUK_STORAGE_BACKEND="filesystem", URUK_STORAGE_ROOT=str(tmp_path)), tenant_context(src):
        svc.upload_asset("main.css", "text/css", b"body{color:blue}")

    # Archive is produced while the filesystem backend is still active (reads the blob).
    with override_settings(URUK_STORAGE_BACKEND="filesystem", URUK_STORAGE_ROOT=str(tmp_path)):
        archive = json.loads(json.dumps(archive_tenant(src)))

    # Restore onto an instance with NO backend: bytes land back in the DB column.
    dst = restore_tenant(archive, schema_name="arch_fs_dst", name="Restored")
    with tenant_context(dst):
        asset = Asset.objects.get(name="main.css")
        assert asset.storage_key == ""
        assert bytes(asset.data) == b"body{color:blue}"
        assert asset.sha256 == hashlib.sha256(b"body{color:blue}").hexdigest()
        assert asset.read_bytes() == b"body{color:blue}"


@pytest.mark.django_db(transaction=True)
def test_restore_rejects_unknown_version() -> None:
    with pytest.raises(ArchiveVersionError):
        restore_tenant({"uruk_archive_version": 999, "tenant": {"name": "x"}}, schema_name="arch_bad")
