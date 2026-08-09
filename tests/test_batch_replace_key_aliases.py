"""Tests for D4: gdoc_batch_replace and text_batch_replace accept each other's key names."""

from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def mock_drive():
    with patch("gsuite_mcp.auth.get_drive_service") as mock:
        service = MagicMock()
        mock.return_value = service
        yield service


@pytest.fixture
def mock_docs():
    with patch("gsuite_mcp.auth.get_docs_service") as mock:
        service = MagicMock()
        mock.return_value = service
        yield service


@pytest.mark.asyncio
async def test_gdoc_batch_replace_accepts_find_replace_alias(mock_drive, mock_docs, monkeypatch):
    monkeypatch.setenv("GDOC_REVIEW_DOC_IDS", "some_other_doc")
    mock_drive.files().get.return_value.execute.return_value = {
        "name": "Doc", "mimeType": "application/vnd.google-apps.document",
        "trashed": False,
    }
    mock_drive.revisions().list.return_value.execute.return_value = {"revisions": []}
    mock_docs.documents().get.return_value.execute.return_value = {
        "body": {"content": [{"endIndex": 1, "paragraph": {"elements": [
            {"startIndex": 0, "endIndex": 1, "textRun": {"content": "Hello\n"}}
        ]}}]}
    }
    mock_docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    from gsuite_mcp.server import gdoc_batch_replace
    result = await gdoc_batch_replace(
        file_id="doc1",
        edits=[{"find": "Hello", "replace": "Hi"}],  # text_batch_replace's key names
        dry_run=True,
    )
    assert "error" not in result


@pytest.mark.asyncio
async def test_text_batch_replace_accepts_find_text_replace_text_alias(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = {
        "name": "notes.md", "mimeType": "text/markdown", "size": "20",
        "modifiedTime": "2026-07-10T00:00:00Z", "md5Checksum": "abc123",
        "trashed": False,
    }
    mock_drive.files().get_media.return_value.execute.return_value = b"Hello world"

    from gsuite_mcp.server import text_batch_replace
    result = await text_batch_replace(
        file_id="f1",
        edits=[{"find_text": "Hello", "replace_text": "Hi"}],  # gdoc_batch_replace's key names
        dry_run=True,
    )
    assert "error" not in result
    assert result["matches_found"] == 1


@pytest.mark.asyncio
async def test_text_batch_replace_still_rejects_missing_keys(mock_drive):
    from gsuite_mcp.server import text_batch_replace
    result = await text_batch_replace(file_id="f1", edits=[{"nope": "x"}])
    assert result["error"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_gdoc_batch_replace_still_rejects_missing_keys(mock_drive, monkeypatch):
    monkeypatch.setenv("GDOC_REVIEW_DOC_IDS", "some_other_doc")
    mock_drive.files().get.return_value.execute.return_value = {
        "name": "Doc", "mimeType": "application/vnd.google-apps.document",
        "trashed": False,
    }
    from gsuite_mcp.server import gdoc_batch_replace
    result = await gdoc_batch_replace(file_id="doc1", edits=[{"nope": "x"}])
    assert result["error"] == "INVALID_INPUT"
