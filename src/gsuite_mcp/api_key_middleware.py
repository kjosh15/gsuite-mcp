"""Starlette middleware that requires a shared-secret bearer token."""

import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Reject requests that don't carry `Authorization: Bearer <api_key>`.

    Comparison is constant-time via hmac.compare_digest. The check is applied
    to every path — there is no health endpoint to exempt and Cloud Run's
    default startup probe is TCP-only, so a 401 here doesn't break startup.
    """

    # OAuth discovery probes an MCP client sends before it knows how to
    # authenticate. This server does NOT do OAuth, so these must 404 (no
    # metadata here) rather than 401. A 401 makes clients like claude.ai
    # treat the server as OAuth-protected and attempt Dynamic Client
    # Registration, which fails ("Couldn't register with the sign-in
    # service") and drops the connector. A clean 404 lets the client fall
    # back to the API-key URL.
    _OAUTH_DISCOVERY_PREFIXES = (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
        "/.well-known/openid-configuration",
    )

    def __init__(self, app, api_key: str) -> None:
        super().__init__(app)
        self._api_key = api_key

    async def dispatch(self, request: Request, call_next) -> Response:
        # Answer OAuth discovery probes with 404 before the auth check —
        # they must never require a credential (see class comment).
        if request.url.path.startswith(self._OAUTH_DISCOVERY_PREFIXES):
            return JSONResponse({"error": "not_found"}, status_code=404)

        # Check Authorization header first, then fall back to ?key= query param
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[len("Bearer ") :]
        else:
            provided = request.query_params.get("key", "")
        if not provided or not hmac.compare_digest(provided, self._api_key):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)
