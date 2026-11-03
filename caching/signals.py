"""Cache-invalidation signal receivers (ARCHITECTURE.md §7)."""

from __future__ import annotations

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from caching.service import invalidate_for_element, invalidate_for_page
from content.models import Element, Page


@receiver(post_save, sender=Element)
@receiver(post_delete, sender=Element)
def invalidate_on_element_change(sender: type[Element], instance: Element, **kwargs: object) -> None:
    """Invalidate cached pages and fragments affected by a saved or deleted element."""
    invalidate_for_element(instance)


@receiver(pre_save, sender=Page)
def remember_previous_parent(sender: type[Page], instance: Page, **kwargs: object) -> None:
    """Stash the page's parent as stored, so a move also refreshes the collection it left."""
    instance._uruk_previous_parent_id = Page.objects.filter(pk=instance.pk).values_list("parent_id", flat=True).first()  # noqa: SLF001 - signal-private scratch attribute


@receiver(post_save, sender=Page)
@receiver(post_delete, sender=Page)
def invalidate_on_page_change(sender: type[Page], instance: Page, **kwargs: object) -> None:
    """Invalidate the page's own cached output and every listing of its collection."""
    invalidate_for_page(instance, previous_parent_id=getattr(instance, "_uruk_previous_parent_id", None))
