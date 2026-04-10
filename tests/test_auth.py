from unittest.mock import patch, MagicMock

import pytest


def test_get_credentials_from_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client123")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret456")
    monkeypatch.setenv("GOOGLE_OAUTH_REFRESH_TOKEN", "refresh789")

    with patch("gdrive_mcp.auth.Credentials") as mock_creds_cls, \
         patch("gdrive_mcp.auth.Request"):
        mock_creds = MagicMock()
        mock_creds_cls.return_value = mock_creds

        from gdrive_mcp.auth import get_credentials, _reset_cache
        _reset_cache()
        creds = get_credentials()

        mock_creds_cls.assert_called_once_with(
            token=None,
            refresh_token="refresh789",
            token_uri="https://oauth2.googleapis.com/token",
            client_id="client123",
            client_secret="secret456",
            scopes=[
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/documents",
                "https://www.googleapis.com/auth/spreadsheets",
            ],
        )
        mock_creds.refresh.assert_called_once()
        assert creds is mock_creds


def test_get_credentials_caches(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client123")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret456")
    monkeypatch.setenv("GOOGLE_OAUTH_REFRESH_TOKEN", "refresh789")

    with patch("gdrive_mcp.auth.Credentials") as mock_creds_cls, \
         patch("gdrive_mcp.auth.Request"):
        mock_creds_cls.return_value = MagicMock()

        from gdrive_mcp.auth import get_credentials, _reset_cache
        _reset_cache()
        get_credentials()
        get_credentials()

        mock_creds_cls.assert_called_once()


def test_get_credentials_missing_env_raises(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_REFRESH_TOKEN", raising=False)

    from gdrive_mcp.auth import get_credentials, _reset_cache, AuthError
    _reset_cache()
    with pytest.raises(AuthError, match="GOOGLE_OAUTH_"):
        get_credentials()


def test_service_factories_use_credentials(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "c")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "s")
    monkeypatch.setenv("GOOGLE_OAUTH_REFRESH_TOKEN", "r")

    with patch("gdrive_mcp.auth.Credentials"), \
         patch("gdrive_mcp.auth.Request"), \
         patch("gdrive_mcp.auth.build") as mock_build:
        from gdrive_mcp.auth import (
            get_drive_service, get_docs_service, get_sheets_service, _reset_cache,
        )
        _reset_cache()

        get_drive_service()
        get_docs_service()
        get_sheets_service()

        assert mock_build.call_args_list[0][0] == ("drive", "v3")
        assert mock_build.call_args_list[1][0] == ("docs", "v1")
        assert mock_build.call_args_list[2][0] == ("sheets", "v4")
