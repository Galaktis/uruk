"""Studio: the minimal human review UI, server-rendered and driven by htmx.

The agent builds; a human reviews, fixes, and publishes. Every view here is a thin
wrapper over the same service layer as the API and MCP — edits land in the working
copy, the preview pane shows ``/_preview/`` (the production renderers), and publishing
goes through :mod:`services.publishing`. Authorization is Django's permissions, checked
per action, against the signed-in session (log in through the admin, at ``/<URUK_ADMIN_PATH>/``).

htmx contract: mutating endpoints answer with the refreshed fragment plus an
``HX-Trigger: content-changed`` header; the status bar, the element tree, and the
preview iframe all listen for that event, so one edit refreshes everything in view.
"""

from __future__ import annotations

import json
from functools import wraps
from typing import TYPE_CHECKING, cast

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods

from common.users import authenticated_user, request_user
from content.models import Element, Page, Revision
from rendering.sources import DatabaseSource
from services import content as svc
from services import publishing
from services.content import HOME_SLUG

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from django.http import HttpRequest

#: The event every mutation fires (``HX-Trigger``) so dependent panes refresh.
CHANGED_EVENT = "content-changed"
#: Characters of an element's content shown on its tree node.
PREVIEW_CHARS = 60


def studio_view[View: Callable[..., HttpResponse]](perm: str) -> Callable[[View], View]:
    """Require a signed-in session (else the admin login) holding the Django permission ``perm``."""

    def decorator(view: View) -> View:
        @wraps(view)
        def wrapper(request: HttpRequest, *args: object, **kwargs: object) -> HttpResponse:
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not request_user(request).has_perm(perm):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return cast("View", wrapper)  # same signature: it only adds the checks

    return decorator


# Full page -------------------------------------------------------------------
@require_http_methods(["GET"])
@studio_view("content.view_page")
def home(request: HttpRequest, page_id: UUID | None = None) -> HttpResponse:
    """Render the studio for one page (default: the home page): pages, tree, editor, preview."""
    pages = svc.list_pages()
    page = get_object_or_404(Page, pk=page_id) if page_id else _default_page(pages)
    source = DatabaseSource()
    context = {
        "page_tree": _page_tree(pages),
        "page": page,
        "tree": _element_tree(page.root_element_id) if page and page.root_element_id else None,
        "preview_url": "/_preview" + source.page_path(page) if page else "",
        **_status_context(),
    }
    return render(request, "studio/studio.html", context)


def _default_page(pages: list[Page]) -> Page | None:
    """Pick the page the studio opens on: the home page, else the first page."""
    return next((page for page in pages if page.parent_id is None and page.slug == HOME_SLUG), pages[0] if pages else None)


# Partials --------------------------------------------------------------------
@require_http_methods(["GET"])
@studio_view("content.view_revision")
def status(request: HttpRequest) -> HttpResponse:
    """Render the status bar: mode, live revision, unpublished changes, publish/history controls."""
    return render(request, "studio/_status.html", _status_context())


@require_http_methods(["GET"])
@studio_view("content.view_element")
def tree(request: HttpRequest, element_id: UUID) -> HttpResponse:
    """Render the element tree rooted at ``element_id`` (also how a shared ``reference`` is opened)."""
    return render(request, "studio/_tree.html", {"tree": _element_tree(element_id)})


@studio_view("content.view_element")
def element(request: HttpRequest, element_id: UUID) -> HttpResponse:
    """Show (GET) or save (POST) an element's content and parameters."""
    instance = get_object_or_404(Element, pk=element_id)
    if request.method != "POST":
        return _editor(request, instance)
    if not request_user(request).has_perm("content.change_element"):
        raise PermissionDenied
    content = request.POST.get("content", "")
    try:
        parameters = json.loads(request.POST.get("parameters") or "{}")
    except json.JSONDecodeError as exc:
        return _editor(request, instance, error=f"Parameters are not valid JSON: {exc}", content=content, parameters=request.POST.get("parameters", ""))
    if not isinstance(parameters, dict):
        return _editor(request, instance, error="Parameters must be a JSON object.", content=content, parameters=request.POST.get("parameters", ""))
    svc.update_element(instance, content=content, parameters=parameters)
    return _changed(_editor(request, instance, saved=True))


@require_http_methods(["POST"])
@studio_view("content.delete_element")
def delete_element(request: HttpRequest, element_id: UUID) -> HttpResponse:
    """Delete an element and its subtree."""
    svc.delete_element(get_object_or_404(Element, pk=element_id))
    return _changed(HttpResponse('<p class="hint">Element deleted.</p>'))


@require_http_methods(["POST"])
@studio_view("content.change_element")
def move_element(request: HttpRequest, element_id: UUID) -> HttpResponse:
    """Swap an element with its previous (``direction=up``) or next sibling."""
    instance = get_object_or_404(Element, pk=element_id)
    if instance.parent is not None:
        siblings = [child.pk for child in instance.parent.children.all()]
        index = siblings.index(instance.pk)
        other = index - 1 if request.POST.get("direction") == "up" else index + 1
        if 0 <= other < len(siblings):
            siblings[index], siblings[other] = siblings[other], siblings[index]
            svc.reorder_children(instance.parent, siblings)
    return _changed(_editor(request, instance))


@require_http_methods(["POST"])
@studio_view("content.add_revision")
def publish(request: HttpRequest) -> HttpResponse:
    """Publish the working copy."""
    publishing.publish(message=request.POST.get("message", "").strip(), author=authenticated_user(request))
    return _changed(render(request, "studio/_status.html", _status_context()))


@require_http_methods(["POST"])
@studio_view("content.discard_draft")
def discard(request: HttpRequest) -> HttpResponse:
    """Throw away unpublished changes (reset the working copy to the live revision)."""
    publishing.discard_draft()
    return _changed(render(request, "studio/_status.html", _status_context()))


@require_http_methods(["POST"])
@studio_view("content.add_revision")
def rollback(request: HttpRequest, revision_id: UUID) -> HttpResponse:
    """Make an older revision live again."""
    publishing.rollback(get_object_or_404(Revision, pk=revision_id), author=authenticated_user(request))
    return _changed(render(request, "studio/_status.html", _status_context()))


# Helpers ---------------------------------------------------------------------
def _changed(response: HttpResponse) -> HttpResponse:
    """Mark a response as a content mutation, so htmx refreshes status, tree, and preview."""
    response["HX-Trigger"] = CHANGED_EVENT
    return response


def _editor(request: HttpRequest, instance: Element, **extra: object) -> HttpResponse:
    """Render the editor panel for one element."""
    context = {
        "element": instance,
        "content": instance.content,
        "parameters": json.dumps(instance.parameters, indent=2, ensure_ascii=False),
        "can_edit": request_user(request).has_perm("content.change_element"),
        "can_delete": request_user(request).has_perm("content.delete_element"),
        **extra,
    }
    return render(request, "studio/_editor.html", context)


def _status_context() -> dict[str, object]:
    """Gather the publishing state and history the status bar shows."""
    state = publishing.status()
    changes = cast("dict[str, dict[str, list]]", state["changes"])
    return {
        "status": state,
        "change_count": sum(len(entries) for collection in changes.values() for entries in collection.values()),
        "revisions": publishing.list_revisions()[:20],
    }


def _page_tree(pages: list[Page]) -> list[dict[str, object]]:
    """Nest pages under their parents (``[{"page", "children"}]``), in tree order."""
    children: dict[UUID | None, list[Page]] = {}
    for page in pages:
        children.setdefault(page.parent_id, []).append(page)

    def nest(parent_id: UUID | None) -> list[dict[str, object]]:
        return [{"page": page, "children": nest(page.pk)} for page in children.get(parent_id, [])]

    return nest(None)


def _element_tree(root_id: UUID) -> dict[str, object] | None:
    """Load an element subtree in one query and nest it for the tree template."""
    root = Element.objects.filter(pk=root_id).first()
    if root is None:
        return None
    children: dict[UUID | None, list[Element]] = {}
    for row in Element.objects.all():
        children.setdefault(row.parent_id, []).append(row)

    def node(instance: Element) -> dict[str, object]:
        text = " ".join(instance.content.split())
        return {
            "element": instance,
            "preview": text[:PREVIEW_CHARS] + ("…" if len(text) > PREVIEW_CHARS else ""),
            "target_id": instance.parameters.get("element_id") if instance.kind == "reference" else None,
            "children": [node(child) for child in children.get(instance.pk, [])],
        }

    return node(root)
