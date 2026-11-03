"""Serving database-stored assets by content hash, and the storage-backend hatch (§9)."""

import io
import sys
import types
from typing import ClassVar

import pytest
from django.http import Http404
from django.test import Client, RequestFactory, override_settings
from django_tenants.utils import tenant_context

from content.models import Asset
from content.views import asset_url, serve_asset
from services import content as svc
from services.storage import get_storage_backend
from tenants.models import Domain, Tenant


@pytest.mark.django_db(transaction=True)
def test_serve_asset_headers_and_conditional() -> None:
    tenant = Tenant.objects.create(schema_name="asset_view", name="AssetView")
    factory = RequestFactory()

    with tenant_context(tenant):
        asset = svc.upload_asset("main.css", "text/css", b"body{color:red}")

        response = serve_asset(factory.get("/"), asset.sha256, "main.css")
        assert response.status_code == 200
        assert response.content == b"body{color:red}"
        assert response["Content-Type"] == "text/css"
        assert response["Cache-Control"] == "public, max-age=31536000, immutable"
        assert response["ETag"] == f'"{asset.sha256}"'
        assert asset_url(asset) == f"/_assets/{asset.sha256}/main.css"

        # A matching If-None-Match yields 304 Not Modified.
        conditional = factory.get("/", headers={"if-none-match": response["ETag"]})
        assert serve_asset(conditional, asset.sha256, "main.css").status_code == 304

        # Unknown hash is a 404.
        with pytest.raises(Http404):
            serve_asset(factory.get("/"), "0" * 64, "missing.css")


@pytest.mark.django_db(transaction=True)
def test_serve_asset_over_http_routed_by_host() -> None:
    tenant = Tenant.objects.create(schema_name="asset_http", name="AssetHttp")
    Domain.objects.create(domain="assets.localhost", tenant=tenant, is_primary=True)
    with tenant_context(tenant):
        asset = svc.upload_asset("app.js", "application/javascript", b"alert(1)")

    # TenantMainMiddleware resolves the tenant from the Host header; the autouse
    # conftest fixture resets the connection to the public schema afterwards.
    response = Client().get(f"/_assets/{asset.sha256}/app.js", headers={"host": "assets.localhost"})
    assert response.status_code == 200
    assert response.content == b"alert(1)"
    assert response["Content-Type"] == "application/javascript"
    assert "immutable" in response["Cache-Control"]


@pytest.mark.django_db(transaction=True)
def test_filesystem_backend_keeps_bytes_out_of_the_db(tmp_path) -> None:  # noqa: ANN001 - pytest tmp_path fixture
    """With a filesystem backend active, Asset.data is empty and bytes are on disk."""
    tenant = Tenant.objects.create(schema_name="asset_fs", name="AssetFS")
    with override_settings(URUK_STORAGE_BACKEND="filesystem", URUK_STORAGE_ROOT=str(tmp_path)), tenant_context(tenant):
        asset = svc.upload_asset("logo.svg", "image/svg+xml", b"<svg/>")

        # The row keeps hash + a storage_key, but no inline bytes.
        row = Asset.objects.get(pk=asset.pk)
        assert bytes(row.data) == b""
        assert row.storage_key == asset.sha256
        # The blob is content-addressed on disk (sharded by the first two hash chars).
        assert (tmp_path / asset.sha256[:2] / asset.sha256).read_bytes() == b"<svg/>"
        # read_bytes() and serving both resolve transparently through the backend.
        assert row.read_bytes() == b"<svg/>"
        assert serve_asset(RequestFactory().get("/"), asset.sha256, "logo.svg").content == b"<svg/>"


class _FakeS3:
    """An in-memory stand-in for a boto3 S3 client; every client shares one bucket store."""

    objects: ClassVar[dict[tuple[str, str], bytes]] = {}

    def __init__(self, **config: object) -> None:
        self.config = config

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:  # noqa: N803 - boto3's argument names
        self.objects[Bucket, Key] = Body
        self.content_type = ContentType

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803 - boto3's argument names
        return {"Body": io.BytesIO(self.objects[Bucket, Key])}


@pytest.mark.django_db(transaction=True)
def test_boto_backend_stores_bytes_in_the_bucket(monkeypatch) -> None:  # noqa: ANN001 - pytest fixture
    """The S3 backend uploads under the prefix and reads back through the same client."""
    clients: list[_FakeS3] = []

    def client(service: str, **config: object) -> _FakeS3:
        assert service == "s3"
        clients.append(_FakeS3(**config))
        return clients[-1]

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=client))
    tenant = Tenant.objects.create(schema_name="asset_s3", name="AssetS3")
    s3 = {"URUK_STORAGE_BUCKET": "uruk", "URUK_STORAGE_PREFIX": "assets/", "URUK_STORAGE_ENDPOINT_URL": "http://minio:9000", "URUK_STORAGE_REGION": ""}
    with override_settings(URUK_STORAGE_BACKEND="boto", **s3), tenant_context(tenant):
        asset = svc.upload_asset("app.js", "application/javascript", b"alert(1)")
        assert Asset.objects.get(pk=asset.pk).read_bytes() == b"alert(1)"

    assert clients[0].config == {"endpoint_url": "http://minio:9000", "region_name": None}
    assert _FakeS3.objects == {("uruk", f"assets/{asset.sha256}"): b"alert(1)"}
    assert clients[0].content_type == "application/javascript"


@pytest.mark.parametrize(
    ("config", "error"),
    [
        ({"URUK_STORAGE_BACKEND": "filesystem", "URUK_STORAGE_ROOT": ""}, "requires URUK_STORAGE_ROOT"),
        ({"URUK_STORAGE_BACKEND": "boto", "URUK_STORAGE_BUCKET": ""}, "requires URUK_STORAGE_BUCKET"),
        ({"URUK_STORAGE_BACKEND": "ftp"}, "Unknown URUK_STORAGE_BACKEND"),
    ],
)
def test_misconfigured_storage_backend_is_refused(config: dict[str, str], error: str) -> None:
    with override_settings(**config), pytest.raises(ValueError, match=error):
        get_storage_backend()
