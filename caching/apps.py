"""Caching app configuration."""

from django.apps import AppConfig


class CachingConfig(AppConfig):
    """Connects cache-invalidation signal receivers at startup."""

    name = "caching"

    def ready(self) -> None:
        """Import signal receivers so they are connected."""
        from caching import signals  # noqa: F401, PLC0415 - import for signal registration
