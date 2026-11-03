#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys


def main() -> None:
    """Run administrative tasks."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

    from config.telemetry import configure_telemetry  # noqa: PLC0415 - keep module import side-effect free

    configure_telemetry()  # no-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set
    try:
        from django.core.management import execute_from_command_line  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        msg = (
            "Couldn't import Django. Are you sure it's installed and available "
            "on your PYTHONPATH environment variable? Did you forget to activate "
            "a virtual environment?"
        )
        raise ImportError(msg) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
