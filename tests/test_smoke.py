"""Smoke tests proving the Django 6 + psycopg3 + Postgres foundation works."""

import pytest
from django.conf import settings
from django.db import connection


@pytest.mark.django_db
def test_database_connection() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        row = cursor.fetchone()
    assert row == (1,)


def test_settings_loaded() -> None:
    assert settings.SECRET_KEY
    assert settings.ROOT_URLCONF == "config.urls"
