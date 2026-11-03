"""Uruk root URL configuration."""

from django.conf import settings
from django.contrib import admin
from django.urls import URLPattern, URLResolver, include, path, re_path

from api.api import api
from content import views as content_views

urlpatterns: list[URLPattern | URLResolver] = [
    path(f"{settings.URUK_ADMIN_PATH}/", admin.site.urls),
    path("api/", api.urls),
    path("_studio/", include("studio.urls")),
    path("_assets/<str:sha256>/<str:name>", content_views.serve_asset),
    path("_fragment/<uuid:element_id>", content_views.serve_fragment),
    # Discovery files. Slugs cannot contain dots, so these never shadow a page.
    path("sitemap.xml", content_views.serve_sitemap),
    path("robots.txt", content_views.serve_robots),
    re_path(r"^(?P<path>.+)/feed\.xml$", content_views.serve_feed),
    # Draft preview of the unpublished working copy, for signed-in editors (§16).
    re_path(r"^_preview/(?P<path>.*)$", content_views.serve_preview),
    # Public site: a catch-all that resolves the request path to a Page in the
    # active tenant and renders it. Must stay LAST so reserved prefixes win.
    re_path(r"^(?P<path>.*)$", content_views.serve_page),
]
