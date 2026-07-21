"""Unit tests for APIKeyMiddleware.

We exercise the middleware's `dispatch` method directly with a fake Request
and a stub call_next, avoiding any need for httpx/TestClient.
"""

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse


def _make_request(
    headers: list[tuple[bytes, bytes]],
    query_string: bytes = b"",
    path: str = "/mcp",
) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": headers,
        "query_string": query_string,
    }
    return Request(scope)


async def _call_next_ok(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok", status_code=200)


@pytest.mark.asyncio
async def test_missing_authorization_header_returns_401():
    from gsuite_mcp.api_key_middleware import APIKeyMiddleware

    middleware = APIKeyMiddleware(app=None, api_key="secret123")
    request = _make_request(headers=[])
    response = await middleware.dispatch(request, _call_next_ok)
    assert response.status_code == 401
    assert b"unauthorized" in response.body


@pytest.mark.asyncio
async def test_wrong_bearer_token_returns_401():
    from gsuite_mcp.api_key_middleware import APIKeyMiddleware

    middleware = APIKeyMiddleware(app=None, api_key="secret123")
    request = _make_request(headers=[(b"authorization", b"Bearer wrongkey")])
    response = await middleware.dispatch(request, _call_next_ok)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_correct_bearer_token_passes_through():
    from gsuite_mcp.api_key_middleware import APIKeyMiddleware

    middleware = APIKeyMiddleware(app=None, api_key="secret123")
    request = _make_request(
        headers=[(b"authorization", b"Bearer secret123")]
    )
    response = await middleware.dispatch(request, _call_next_ok)
    assert response.status_code == 200
    assert response.body == b"ok"


@pytest.mark.asyncio
async def test_missing_bearer_scheme_returns_401():
    from gsuite_mcp.api_key_middleware import APIKeyMiddleware

    middleware = APIKeyMiddleware(app=None, api_key="secret123")
    # Raw token with no "Bearer " prefix should be rejected
    request = _make_request(headers=[(b"authorization", b"secret123")])
    response = await middleware.dispatch(request, _call_next_ok)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_correct_query_param_key_passes_through():
    from gsuite_mcp.api_key_middleware import APIKeyMiddleware

    middleware = APIKeyMiddleware(app=None, api_key="secret123")
    request = _make_request(headers=[], query_string=b"key=secret123")
    response = await middleware.dispatch(request, _call_next_ok)
    assert response.status_code == 200
    assert response.body == b"ok"


@pytest.mark.asyncio
async def test_wrong_query_param_key_returns_401():
    from gsuite_mcp.api_key_middleware import APIKeyMiddleware

    middleware = APIKeyMiddleware(app=None, api_key="secret123")
    request = _make_request(headers=[], query_string=b"key=wrongkey")
    response = await middleware.dispatch(request, _call_next_ok)
    assert response.status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-authorization-server",
        "/.well-known/openid-configuration",
    ],
)
@pytest.mark.asyncio
async def test_oauth_discovery_paths_return_404_not_401(path):
    """OAuth discovery probes must 404 (no OAuth here), never 401.

    A 401 makes MCP clients (e.g. claude.ai) treat the server as
    OAuth-protected and attempt Dynamic Client Registration, which fails
    with 'Couldn't register with the sign-in service' and drops the
    connector. A 404 tells the client there is no OAuth metadata, so it
    falls back to the API-key URL. The check runs *before* the auth check,
    so no credential is required to probe.
    """
    from gsuite_mcp.api_key_middleware import APIKeyMiddleware

    middleware = APIKeyMiddleware(app=None, api_key="secret123")
    request = _make_request(headers=[], path=path)
    response = await middleware.dispatch(request, _call_next_ok)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_non_oauth_wellknown_path_still_requires_key():
    """Only the OAuth discovery family is exempt; other paths still 401."""
    from gsuite_mcp.api_key_middleware import APIKeyMiddleware

    middleware = APIKeyMiddleware(app=None, api_key="secret123")
    request = _make_request(headers=[], path="/mcp/tools")
    response = await middleware.dispatch(request, _call_next_ok)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_bearer_header_takes_precedence_over_query_param():
    from gsuite_mcp.api_key_middleware import APIKeyMiddleware

    middleware = APIKeyMiddleware(app=None, api_key="secret123")
    # Correct header + wrong query param → should pass (header wins)
    request = _make_request(
        headers=[(b"authorization", b"Bearer secret123")],
        query_string=b"key=wrongkey",
    )
    response = await middleware.dispatch(request, _call_next_ok)
    assert response.status_code == 200
