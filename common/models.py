"""Shared abstract model bases."""

import uuid

from django.db import models


class UUID7Model(models.Model):
    """Abstract base giving every model a time-ordered UUIDv7 primary key.

    UUIDv7 is time-ordered, so inserts keep good B-tree locality (unlike random
    UUIDv4), and globally-unique keys let a tenant be archived and restored on
    another Uruk instance without primary-key collisions.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)

    class Meta:
        abstract = True


class TimestampedModel(models.Model):
    """Abstract base adding ``created``/``updated`` timestamps."""

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
