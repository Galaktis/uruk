"""Shared pytest fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from django.db import connection

from common.tenancy import tenant_connection

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _reset_tenant_schema() -> Iterator[None]:
    """Reset the connection to the public schema after each test.

    TenantMainMiddleware (exercised by Client requests) switches the connection to a
    tenant schema and leaves it there, which would break later tests that create
    tenants. Only reset when a connection was actually opened, so non-DB tests are
    unaffected.
    """
    yield
    if connection.connection is not None:
        tenant_connection().set_schema_to_public()
