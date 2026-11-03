"""MCP over streamable HTTP: the remote-connector transport (ARCHITECTURE.md §11).

An ASGI app serving the same tools as the stdio server at ``/mcp``, for hosted agents
that connect to a URL rather than launching a local process. Serve it with Granian::

    granian --interface asgi mcp_server.http:app --port 8001

(the container does this with ``docker-entrypoint.sh mcp``). It is **stateless** — every
request stands alone, so any worker or replica can answer it — and every request must
carry ``Authorization: Bearer <uruk_ token>``; the ``Host`` picks the tenant, as for the
website. Requests without a valid token are refused with 401 before reaching MCP.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

import anyio.to_thread  # noqa: E402  (Django must be configured before model imports)
from django.db import connection  # noqa: E402
from django_tenants.utils import tenant_context  # noqa: E402
from starlette.datastructures import Headers  # noqa: E402

from mcp_server.context import build_context_from_headers  # noqa: E402
from mcp_server.server import mcp  # noqa: E402
from services import auth as auth_svc  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping

    Scope = MutableMapping[str, object]
    Message = MutableMapping[str, object]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def _token_is_valid(headers: dict[str, str]) -> bool:
    """Return whether the request's bearer token authenticates a user of the Host's tenant."""
    try:
        agent = build_context_from_headers(headers)
        with tenant_context(agent.tenant):
            return auth_svc.resolve_token(agent.token) is not None
    finally:
        connection.close()  # runs in a worker thread: hand its connection back


class BearerTokenMiddleware:
    """Reject HTTP requests whose bearer token does not authenticate (401), before MCP runs.

    Tools re-check the token and the user's permissions on every call; this gate just
    turns "no or bad credentials" into a proper HTTP 401 for MCP clients.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the MCP ASGI app."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Authenticate ``http`` requests; pass lifespan events straight through."""
        if scope["type"] == "http":
            headers = dict(Headers(scope=scope))  # keys arrive lowercased per the ASGI spec
            if not await anyio.to_thread.run_sync(_token_is_valid, headers):
                await _unauthorized(send)
                return
        await self.app(scope, receive, send)


async def _unauthorized(send: Send) -> None:
    """Send a JSON 401 with a ``WWW-Authenticate: Bearer`` challenge."""
    body = json.dumps({"error": "invalid_token", "error_description": "A valid Uruk API token is required"}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b'Bearer error="invalid_token"'),
                (b"content-length", str(len(body)).encode()),
            ],
        },
    )
    await send({"type": "http.response.body", "body": body})


def create_app() -> BearerTokenMiddleware:
    """Build a fresh ASGI app (its session manager runs once per app lifespan).

    Tenant hosts are arbitrary, so the SDK's localhost-only DNS-rebinding guard must stay
    off (``host="0.0.0.0"`` opts out); the bearer token is the real gate — a rebinding
    page in a victim's browser has no way to attach it.
    """
    return BearerTokenMiddleware(mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0"))  # noqa: S104 - see docstring


app = create_app()
