"""OAuth user credential loading for Google Drive, Docs, and Sheets APIs."""

import os
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]

_cached_credentials: Optional[Credentials] = None


class AuthError(RuntimeError):
    """Raised when OAuth credentials cannot be loaded or refreshed."""


def _reset_cache() -> None:
    """Test helper to clear cached credentials."""
    global _cached_credentials
    _cached_credentials = None


def get_credentials() -> Credentials:
    """Load OAuth user credentials from env vars. Cached after first call."""
    global _cached_credentials
    if _cached_credentials is not None:
        return _cached_credentials

    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")

    missing = [
        name for name, val in [
            ("GOOGLE_OAUTH_CLIENT_ID", client_id),
            ("GOOGLE_OAUTH_CLIENT_SECRET", client_secret),
            ("GOOGLE_OAUTH_REFRESH_TOKEN", refresh_token),
        ]
        if not val
    ]
    if missing:
        raise AuthError(
            f"Missing required OAuth env vars: {', '.join(missing)}. "
            "Run `python -m gdrive_mcp.auth_setup` to generate a refresh token."
        )

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    _cached_credentials = creds
    return creds


def get_drive_service():
    return build("drive", "v3", credentials=get_credentials())


def get_docs_service():
    return build("docs", "v1", credentials=get_credentials())


def get_sheets_service():
    return build("sheets", "v4", credentials=get_credentials())
