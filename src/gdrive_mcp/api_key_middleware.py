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

    def __init__(self, app, api_key: str) -> None:
        super().__init__(app)
        self._api_key = api_key

    async def dispatch(self, request: Request, call_next) -> Response:
        # Check Authorization header first, then fall back to ?key= query param
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[len("Bearer ") :]
        else:
            provided = request.query_params.get("key", "")
        if not provided or not hmac.compare_digest(provided, self._api_key):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)
