"""Unit tests for APIKeyMiddleware.

We exercise the middleware's `dispatch` method directly with a fake Request
and a stub call_next, avoiding any need for httpx/TestClient.
"""

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse


def _make_request(
    headers: list[tuple[bytes, bytes]], query_string: bytes = b""
) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": headers,
        "query_string": query_string,
    }
    return Request(scope)


async def _call_next_ok(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok", status_code=200)


@pytest.mark.asyncio
async def test_missing_authorization_header_returns_401():
    from gdrive_mcp.api_key_middleware import APIKeyMiddleware

    middleware = APIKeyMiddleware(app=None, api_key="secret123")
    request = _make_request(headers=[])
    response = await middleware.dispatch(request, _call_next_ok)
    assert response.status_code == 401
    assert b"unauthorized" in response.body


@pytest.mark.asyncio
async def test_wrong_bearer_token_returns_401():
    from gdrive_mcp.api_key_middleware import APIKeyMiddleware

    middleware = APIKeyMiddleware(app=None, api_key="secret123")
    request = _make_request(headers=[(b"authorization", b"Bearer wrongkey")])
    response = await middleware.dispatch(request, _call_next_ok)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_correct_bearer_token_passes_through():
    from gdrive_mcp.api_key_middleware import APIKeyMiddleware

    middleware = APIKeyMiddleware(app=None, api_key="secret123")
    request = _make_request(
        headers=[(b"authorization", b"Bearer secret123")]
    )
    response = await middleware.dispatch(request, _call_next_ok)
    assert response.status_code == 200
    assert response.body == b"ok"


@pytest.mark.asyncio
async def test_missing_bearer_scheme_returns_401():
    from gdrive_mcp.api_key_middleware import APIKeyMiddleware

    middleware = APIKeyMiddleware(app=None, api_key="secret123")
    # Raw token with no "Bearer " prefix should be rejected
    request = _make_request(headers=[(b"authorization", b"secret123")])
    response = await middleware.dispatch(request, _call_next_ok)
    assert response.status_code == 401
