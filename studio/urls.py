"""Studio URL routes, mounted under ``/_studio/``."""

from django.urls import path

from studio import views

app_name = "studio"

urlpatterns = [
    path("", views.home, name="home"),
    path("pages/<uuid:page_id>", views.home, name="page"),
    path("status", views.status, name="status"),
    path("elements/<uuid:element_id>", views.element, name="element"),
    path("elements/<uuid:element_id>/tree", views.tree, name="tree"),
    path("elements/<uuid:element_id>/delete", views.delete_element, name="delete"),
    path("elements/<uuid:element_id>/move", views.move_element, name="move"),
    path("publish", views.publish, name="publish"),
    path("discard", views.discard, name="discard"),
    path("revisions/<uuid:revision_id>/rollback", views.rollback, name="rollback"),
]
