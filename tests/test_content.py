"""Content models work inside a tenant schema: element tree, assets, settings."""

import hashlib
import uuid

import pytest
from django_tenants.utils import tenant_context

from content.models import Asset, Element, Page, Setting
from tenants.models import Tenant


@pytest.mark.django_db(transaction=True)
def test_page_element_tree_roundtrip() -> None:
    tenant = Tenant.objects.create(schema_name="ct_tree", name="Tree")

    with tenant_context(tenant):
        root = Element.objects.create(kind="container")
        Element.objects.create(kind="text", parent=root, position=0, content="<p>Hello</p>")
        Element.objects.create(kind="text", parent=root, position=1, content="<p>World</p>")
        Page.objects.create(slug="index", name="Index", root_element=root)

        fetched = Page.objects.get(slug="index")
        assert fetched.root_element_id == root.id

        children = list(fetched.root_element.children.all())
        assert [c.content for c in children] == ["<p>Hello</p>", "<p>World</p>"]
        # Ordering comes from Meta.ordering = ["position", ...].
        assert [c.position for c in children] == [0, 1]
        # Primary keys are UUIDv7.
        assert all(isinstance(c.pk, uuid.UUID) and c.pk.version == 7 for c in children)


@pytest.mark.django_db(transaction=True)
def test_asset_hash_and_setting() -> None:
    tenant = Tenant.objects.create(schema_name="ct_misc", name="Misc")

    with tenant_context(tenant):
        css = Asset.objects.create(name="main.css", content_type="text/css", data=b"body{margin:0}")
        assert css.sha256 == hashlib.sha256(b"body{margin:0}").hexdigest()

        Setting.objects.create(key="site.title", value={"text": "Uruk"})
        assert Setting.objects.get(key="site.title").value == {"text": "Uruk"}
