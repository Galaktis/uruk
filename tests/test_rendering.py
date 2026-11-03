"""The renderer turns an element tree into HTML, with escaping and dependency tracking."""

import pytest
from django_tenants.utils import tenant_context

from content.models import Asset, Element, Page
from rendering import RenderContext, UnknownElementKindError, render_element, render_page
from tenants.models import Tenant


@pytest.mark.django_db(transaction=True)
def test_render_page_composes_element_tree() -> None:
    tenant = Tenant.objects.create(schema_name="render_t", name="Render")

    with tenant_context(tenant):
        root = Element.objects.create(kind="container", parameters={"tag": "section", "class": "main"})
        Element.objects.create(kind="heading", parent=root, position=0, parameters={"level": 2}, content="Hi")
        Element.objects.create(kind="text", parent=root, position=1, content="Hello <b>world</b>")
        page = Page.objects.create(slug="index", name="Index", root_element=root)

        ctx = RenderContext()
        html = render_page(page, ctx)

        assert html == '<section class="main"><h2>Hi</h2><p>Hello &lt;b&gt;world&lt;/b&gt;</p></section>'
        # Editor text is escaped — no XSS.
        assert "&lt;b&gt;" in html
        # Every visited element is recorded for cache invalidation (§7).
        assert ctx.dependencies == {root.pk} | {c.pk for c in root.children.all()}
        assert len(ctx.dependencies) == 3


@pytest.mark.django_db(transaction=True)
def test_reference_includes_target_and_records_its_dependencies() -> None:
    tenant = Tenant.objects.create(schema_name="render_ref", name="Ref")

    with tenant_context(tenant):
        header = Element.objects.create(kind="raw_html", content="<header>shared</header>", is_reusable=True)
        root = Element.objects.create(kind="container")
        reference = Element.objects.create(kind="reference", parent=root, parameters={"element_id": str(header.pk)})

        ctx = RenderContext()
        html = render_element(root, ctx)

        assert "<header>shared</header>" in html
        # The target joins the dependency set, so editing it evicts referencing pages (§7).
        assert {root.pk, reference.pk, header.pk} <= ctx.dependencies


@pytest.mark.django_db(transaction=True)
def test_reference_tolerates_dangling_malformed_and_cyclic_targets() -> None:
    tenant = Tenant.objects.create(schema_name="render_refbad", name="RefBad")

    with tenant_context(tenant):
        dangling = Element.objects.create(kind="reference", parameters={"element_id": "01980000-0000-7000-8000-000000000000"})
        assert render_element(dangling, RenderContext()) == ""

        malformed = Element.objects.create(kind="reference", parameters={"element_id": "not-a-uuid"})
        assert render_element(malformed, RenderContext()) == ""

        missing = Element.objects.create(kind="reference")
        assert render_element(missing, RenderContext()) == ""

        # A reference cycle terminates: each target is cut on re-entry, so the loop
        # unrolls exactly once (a → b → a with a dead reference) instead of recursing.
        a = Element.objects.create(kind="container")
        b = Element.objects.create(kind="container")
        Element.objects.create(kind="reference", parent=a, parameters={"element_id": str(b.pk)})
        Element.objects.create(kind="reference", parent=b, parameters={"element_id": str(a.pk)})
        assert render_element(a, RenderContext()) == "<div><div><div></div></div></div>"


@pytest.mark.django_db(transaction=True)
def test_image_renders_from_asset_or_src() -> None:
    tenant = Tenant.objects.create(schema_name="render_img", name="Img")

    with tenant_context(tenant):
        asset = Asset.objects.create(name="logo.png", content_type="image/png", data=b"\x89PNG")
        from_asset = Element.objects.create(kind="image", parameters={"asset_id": str(asset.pk), "alt": "The logo"})
        assert render_element(from_asset, RenderContext()) == f'<img src="/_assets/{asset.sha256}/logo.png" alt="The logo">'

        external = Element.objects.create(kind="image", parameters={"src": "https://example.com/x.png", "class": "hero"})
        assert render_element(external, RenderContext()) == '<img src="https://example.com/x.png" alt="" class="hero">'

        # Dangling, malformed, or absent sources render as nothing — like a dangling reference.
        dangling = Element.objects.create(kind="image", parameters={"asset_id": "01980000-0000-7000-8000-000000000000"})
        assert render_element(dangling, RenderContext()) == ""
        malformed = Element.objects.create(kind="image", parameters={"asset_id": "not-a-uuid"})
        assert render_element(malformed, RenderContext()) == ""
        sourceless = Element.objects.create(kind="image")
        assert render_element(sourceless, RenderContext()) == ""


@pytest.mark.django_db(transaction=True)
def test_nav_list_and_links_compose() -> None:
    tenant = Tenant.objects.create(schema_name="render_nav", name="Nav")

    with tenant_context(tenant):
        nav = Element.objects.create(kind="nav", parameters={"class": "main-nav"})
        menu = Element.objects.create(kind="list", parent=nav)
        home = Element.objects.create(kind="list_item", parent=menu, position=0)
        Element.objects.create(kind="link", parent=home, parameters={"href": "/"}, content="Home <b>!</b>")
        about = Element.objects.create(kind="list_item", parent=menu, position=1)
        Element.objects.create(kind="link", parent=about, parameters={"href": "/about"}, content="About")

        html = render_element(nav, RenderContext())
        # Link labels are escaped — no XSS through menu text.
        assert html == ('<nav class="main-nav"><ul><li><a href="/">Home &lt;b&gt;!&lt;/b&gt;</a></li><li><a href="/about">About</a></li></ul></nav>')


@pytest.mark.django_db(transaction=True)
def test_list_and_link_variants() -> None:
    tenant = Tenant.objects.create(schema_name="render_list", name="List")

    with tenant_context(tenant):
        ordered = Element.objects.create(kind="list", parameters={"ordered": True})
        Element.objects.create(kind="list_item", parent=ordered, content="first")
        assert render_element(ordered, RenderContext()) == "<ol><li>first</li></ol>"

        no_href = Element.objects.create(kind="link", content="label")
        assert render_element(no_href, RenderContext()) == ""

        # A link's label can be composed children (e.g. an image) instead of text.
        logo_link = Element.objects.create(kind="link", parameters={"href": "/home", "class": "logo"})
        Element.objects.create(kind="image", parent=logo_link, parameters={"src": "/logo.svg", "alt": "Ki"})
        assert render_element(logo_link, RenderContext()) == '<a href="/home" class="logo"><img src="/logo.svg" alt="Ki"></a>'


@pytest.mark.django_db(transaction=True)
def test_raw_html_passthrough_and_unknown_kind() -> None:
    tenant = Tenant.objects.create(schema_name="render_raw", name="Raw")

    with tenant_context(tenant):
        raw = Element.objects.create(kind="raw_html", content="<div id='x'>ok</div>")
        assert render_element(raw, RenderContext()) == "<div id='x'>ok</div>"

        unknown = Element.objects.create(kind="does-not-exist")
        with pytest.raises(UnknownElementKindError):
            render_element(unknown, RenderContext())
