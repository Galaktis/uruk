"""Shared model field validators."""

from django.core.exceptions import ValidationError

LOWERCASE_MESSAGE = "Only lowercase strings are allowed."


def validate_lowercase(value: str) -> None:
    """Reject any value containing uppercase characters."""
    if value != value.lower():
        raise ValidationError(LOWERCASE_MESSAGE, code="not_lowercase")
