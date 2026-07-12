"""Tests for the text_replace tool wrapper in server.py."""

from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def mock_drive():
    with patch("gsuite_mcp.auth.get_drive_service") as mock:
        drive = MagicMock()
        mock.return_value = drive
        yield drive


def _live_meta(**overrides):
    base = {
        "name": "notes.md",
        "mimeType": "text/markdown",
        "size": "11",
        "modifiedTime": "2026-07-10T00:00:00Z",
        "md5Checksum": "abc123",
        "trashed": False,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_text_replace_refuses_trashed(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = _live_meta(
        trashed=True, trashedTime="2026-07-01T00:00:00Z",
    )
    from gsuite_mcp.server import text_replace
    result = await text_replace("f1", "old", "new")
    assert result["error"] == "TRASHED_FILE"


@pytest.mark.asyncio
async def test_text_replace_refuses_google_doc(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = _live_meta(
        mimeType="application/vnd.google-apps.document",
    )
    from gsuite_mcp.server import text_replace
    result = await text_replace("f1", "old", "new")
    assert result["error"] == "UNSUPPORTED_MIME"
    assert "replace_text" in result["message"]


@pytest.mark.asyncio
async def test_text_replace_success(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = _live_meta()
    mock_drive.files().get_media.return_value.execute.return_value = b"hello world"
    mock_drive.files().update.return_value.execute.return_value = {
        "id": "f1", "name": "notes.md", "webViewLink": "https://x",
        "version": "2", "modifiedTime": "2026-07-10T01:00:00Z",
    }
    call_count = {"n": 0}
    def revisions_execute():
        call_count["n"] += 1
        return {"revisions": [{"id": f"rev{call_count['n']}"}]}
    revisions_mock = MagicMock()
    revisions_mock.execute = revisions_execute
    mock_drive.revisions().list.return_value = revisions_mock

    from gsuite_mcp.server import text_replace
    result = await text_replace(
        "f1", "world", "there", expected_count=1,
    )
    assert "error" not in result
    assert result["file_id"] == "f1"
    assert result["file_name"] == "notes.md"
    assert result["mime_type"] == "text/markdown"
    assert result["matches_found"] == 1
    mock_drive.files().update.assert_called_once()


@pytest.mark.asyncio
async def test_text_replace_count_mismatch_no_write(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = _live_meta(size="7")
    mock_drive.files().get_media.return_value.execute.return_value = b"foo foo"

    from gsuite_mcp.server import text_replace
    result = await text_replace("f1", "foo", "bar", expected_count=1)
    assert result["error"] == "COUNT_MISMATCH"
    mock_drive.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_text_replace_dry_run(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = _live_meta()
    mock_drive.files().get_media.return_value.execute.return_value = b"hello world"

    from gsuite_mcp.server import text_replace
    result = await text_replace("f1", "world", "there", dry_run=True)
    assert result["dry_run"] is True
    assert result["matches_found"] == 1
    mock_drive.files().update.assert_not_called()
