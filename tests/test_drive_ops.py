"""Tests for drive_ops — trashed-file metadata and trash/untrash operations."""

import pytest
from unittest.mock import MagicMock

from gsuite_mcp import drive_ops


def _mock_drive_service(meta_response):
    svc = MagicMock()
    svc.files().get.return_value.execute.return_value = meta_response
    return svc


# -------------------------------------------------------------------
# get_file_metadata — trashed field
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_file_metadata_trashed_true():
    svc = _mock_drive_service({
        "id": "f1", "name": "Trashed Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-05-19T00:00:00Z", "trashed": True,
        "trashedTime": "2026-05-06T18:00:00Z",
        "webViewLink": "https://...", "parents": [], "capabilities": {},
    })
    result = await drive_ops.get_file_metadata(svc, "f1")
    assert result["trashed"] is True
    assert result["trashed_time"] == "2026-05-06T18:00:00Z"


@pytest.mark.asyncio
async def test_get_file_metadata_trashed_false():
    svc = _mock_drive_service({
        "id": "f1", "name": "Live Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-05-19T00:00:00Z", "trashed": False,
        "webViewLink": "https://...", "parents": [], "capabilities": {},
    })
    result = await drive_ops.get_file_metadata(svc, "f1")
    assert result["trashed"] is False
    assert result.get("trashed_time") is None


@pytest.mark.asyncio
async def test_get_file_metadata_trashed_absent():
    """When API doesn't return trashed field, default to False."""
    svc = _mock_drive_service({
        "id": "f1", "name": "Old Doc", "mimeType": "text/plain",
        "modifiedTime": "2026-05-19T00:00:00Z",
        "webViewLink": "https://...", "parents": [], "capabilities": {},
    })
    result = await drive_ops.get_file_metadata(svc, "f1")
    assert result["trashed"] is False


# -------------------------------------------------------------------
# search_files — trashed field
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_files_includes_trashed_field():
    svc = MagicMock()
    svc.files().list.return_value.execute.return_value = {
        "files": [
            {"id": "f1", "name": "Live", "mimeType": "text/plain",
             "modifiedTime": "2026-05-19T00:00:00Z", "trashed": False,
             "webViewLink": "https://...", "parents": []},
            {"id": "f2", "name": "Dead", "mimeType": "text/plain",
             "modifiedTime": "2026-05-19T00:00:00Z", "trashed": True,
             "trashedTime": "2026-05-10T00:00:00Z",
             "webViewLink": "https://...", "parents": []},
        ]
    }
    result = await drive_ops.search_files(svc, "name contains 'test'")
    assert result["files"][0]["trashed"] is False
    assert result["files"][1]["trashed"] is True
    assert result["files"][1]["trashed_time"] == "2026-05-10T00:00:00Z"


# -------------------------------------------------------------------
# download_file — trashed field
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_file_includes_trashed_field():
    svc = MagicMock()
    svc.files().get.return_value.execute.return_value = {
        "name": "test.txt", "mimeType": "text/plain", "size": "5",
        "trashed": True, "trashedTime": "2026-05-06T00:00:00Z",
    }
    svc.files().get_media.return_value.execute.return_value = b"hello"
    result = await drive_ops.download_file(svc, "f1")
    assert result["trashed"] is True
    assert result["trashed_time"] == "2026-05-06T00:00:00Z"


# -------------------------------------------------------------------
# trash_file / untrash_file
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trash_file():
    svc = MagicMock()
    svc.files().update.return_value.execute.return_value = {
        "id": "f1", "trashed": True, "trashedTime": "2026-05-19T14:30:00Z",
    }
    result = await drive_ops.trash_file(svc, "f1")
    assert result["file_id"] == "f1"
    assert result["trashed"] is True
    assert result["trashed_time"] == "2026-05-19T14:30:00Z"
    call_args = svc.files().update.call_args
    assert call_args.kwargs["body"] == {"trashed": True}


@pytest.mark.asyncio
async def test_untrash_file():
    svc = MagicMock()
    svc.files().update.return_value.execute.return_value = {
        "id": "f1", "trashed": False,
    }
    result = await drive_ops.untrash_file(svc, "f1")
    assert result["file_id"] == "f1"
    assert result["trashed"] is False
    call_args = svc.files().update.call_args
    assert call_args.kwargs["body"] == {"trashed": False}


# -------------------------------------------------------------------
# download_file_bytes
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_file_bytes_returns_raw_bytes():
    svc = MagicMock()
    svc.files().get_media.return_value.execute.return_value = b"raw file content"
    result = await drive_ops.download_file_bytes(svc, "f1")
    assert result == b"raw file content"
    svc.files().get_media.assert_called_with(fileId="f1")
