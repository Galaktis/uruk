"""Typed access to the user of a request.

Django types ``request.user`` as ``AbstractBaseUser | AnonymousUser``, but
``AbstractBaseUser`` lacks the permission API (``has_perm``, ``is_superuser``). Uruk keeps
Django's default ``auth.User`` in every tenant schema, so these helpers narrow the type to
the concrete classes once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied

if TYPE_CHECKING:
    from django.contrib.auth.models import AnonymousUser
    from django.http import HttpRequest


def request_user(request: HttpRequest) -> User | AnonymousUser:
    """Return the request's user, signed in or anonymous."""
    return cast("User | AnonymousUser", request.user)


def authenticated_user(request: HttpRequest) -> User:
    """Return the signed-in user of a request that has already passed authentication.

    Raises :class:`PermissionDenied` if it somehow has not, rather than letting an
    anonymous user through as an author.
    """
    user = request.user
    if not isinstance(user, User):
        raise PermissionDenied
    return user
