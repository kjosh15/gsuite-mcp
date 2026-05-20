"""Tests for trashed-file mutation refusal in server tool wrappers."""

import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture
def trashed_meta():
    """Metadata response for a trashed Google Doc."""
    return {
        "name": "Trashed Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-05-19T00:00:00Z",
        "trashed": True,
        "trashedTime": "2026-05-06T18:00:00Z",
    }


@pytest.fixture
def mock_drive(trashed_meta):
    with patch("gsuite_mcp.auth.get_drive_service") as mock:
        drive = MagicMock()
        mock.return_value = drive
        drive.files().get.return_value.execute.return_value = trashed_meta
        yield drive


@pytest.fixture
def mock_docs():
    with patch("gsuite_mcp.auth.get_docs_service") as mock:
        docs = MagicMock()
        mock.return_value = docs
        yield docs


@pytest.mark.asyncio
async def test_replace_text_refuses_trashed(mock_drive, mock_docs):
    from gsuite_mcp.server import replace_text
    result = await replace_text("f1", "old", "new")
    assert result["error"] == "TRASHED_FILE"
    assert result["file_id"] == "f1"
    assert "trashed_time" in result


@pytest.mark.asyncio
async def test_replace_section_refuses_trashed(mock_drive, mock_docs):
    from gsuite_mcp.server import replace_section
    result = await replace_section("f1", "Heading", "New content")
    assert result["error"] == "TRASHED_FILE"


@pytest.mark.asyncio
async def test_format_document_refuses_trashed(mock_drive, mock_docs):
    from gsuite_mcp.server import format_document
    result = await format_document("f1", [{"action": "set_style", "find_text": "x", "style": "HEADING_1"}])
    assert result["error"] == "TRASHED_FILE"


@pytest.mark.asyncio
async def test_append_to_file_refuses_trashed(mock_drive, mock_docs):
    from gsuite_mcp.server import append_to_file
    result = await append_to_file("f1", "new content")
    assert result["error"] == "TRASHED_FILE"


@pytest.mark.asyncio
async def test_manage_comments_create_refuses_trashed(mock_drive, mock_docs):
    from gsuite_mcp.server import manage_comments
    result = await manage_comments("f1", "create", content="hello")
    assert result["error"] == "TRASHED_FILE"


@pytest.mark.asyncio
async def test_manage_comments_list_allows_trashed(mock_drive, mock_docs):
    """List action should work on trashed files (read-only)."""
    from gsuite_mcp.server import manage_comments
    mock_drive.comments().list.return_value.execute.return_value = {"comments": []}
    result = await manage_comments("f1", "list")
    assert "error" not in result


@pytest.mark.asyncio
async def test_docx_suggest_edit_refuses_trashed(mock_drive, mock_docs):
    """docx_suggest_edit should refuse trashed files."""
    from gsuite_mcp.server import docx_suggest_edit
    # Override mime to docx so it passes MIME check
    mock_drive.files().get.return_value.execute.return_value = {
        "name": "test.docx",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "size": "100",
        "trashed": True,
        "trashedTime": "2026-05-06T18:00:00Z",
    }
    result = await docx_suggest_edit("f1", "old", "new")
    assert result["error"] == "TRASHED_FILE"


@pytest.mark.asyncio
async def test_upload_file_update_refuses_trashed(mock_drive, mock_docs):
    """upload_file in update mode should refuse trashed files."""
    from gsuite_mcp.server import upload_file
    import base64
    content = base64.b64encode(b"hello").decode()
    result = await upload_file(content, "test.txt", "text/plain", file_id="f1")
    assert result["error"] == "TRASHED_FILE"


@pytest.mark.asyncio
async def test_upload_file_create_allows_no_check(mock_drive, mock_docs):
    """upload_file in create mode (no file_id) should not check trashed."""
    from gsuite_mcp.server import upload_file
    import base64
    mock_drive.files().create.return_value.execute.return_value = {
        "id": "new1", "name": "test.txt", "webViewLink": "https://...",
        "version": "1", "modifiedTime": "2026-05-19T00:00:00Z",
    }
    # get for size check after upload
    mock_drive.files().get.return_value.execute.return_value = {"size": "5"}
    content = base64.b64encode(b"hello").decode()
    result = await upload_file(content, "test.txt", "text/plain")
    assert "error" not in result
    assert result["file_id"] == "new1"
