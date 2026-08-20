"""Tests for the /warmup custom route.

/warmup exists to pre-warm the OAuth credential cache (auth.get_credentials)
ahead of a scheduled job's first real tool call — see auth.py's
module-level _cached_credentials. It must call get_credentials() and
nothing else (no Drive/Docs/Gmail API call), so it stays a genuinely
cheap warmup ping.
"""

from unittest.mock import MagicMock, patch

import pytest
from starlette.requests import Request


def _make_request() -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/warmup",
        "headers": [],
        "query_string": b"",
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_warmup_route_calls_get_credentials_and_returns_200():
    from gsuite_mcp.server import warmup

    with patch("gsuite_mcp.server.auth.get_credentials") as mock_get_creds:
        mock_get_creds.return_value = MagicMock()
        response = await warmup(_make_request())

    mock_get_creds.assert_called_once()
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_warmup_route_registered_on_mcp_app():
    """The /warmup route is registered as a FastMCP custom route (GET)."""
    from gsuite_mcp.server import mcp

    routes = mcp._additional_http_routes
    matching = [r for r in routes if r.path == "/warmup"]
    assert len(matching) == 1
    assert "GET" in matching[0].methods
