"""WSGI config for Uruk."""

import os

from django.core.wsgi import get_wsgi_application

from config.telemetry import configure_telemetry

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Per-worker: each Granian worker imports this module in its own process, so the
# exporter's background thread and the instrumentation are set up post-fork.
configure_telemetry()

application = get_wsgi_application()
