from unittest.mock import patch, MagicMock

import pytest

from tests.fixtures.sample_docx import make_docx


@pytest.fixture
def mock_drive():
    with patch("gsuite_mcp.auth.get_drive_service") as mock:
        service = MagicMock()
        mock.return_value = service
        yield service


@pytest.mark.asyncio
async def test_docx_suggest_edit_roundtrips(mock_drive):
    original = make_docx([("The quick brown fox", None)])

    mock_drive.files().get.return_value.execute.return_value = {
        "name": "doc.docx",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "size": str(len(original)),
    }
    mock_drive.files().get_media.return_value.execute.return_value = original
    mock_drive.files().update.return_value.execute.return_value = {
        "id": "d1", "name": "doc.docx",
        "webViewLink": "https://example.com",
        "version": "2",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }

    from gsuite_mcp.server import docx_suggest_edit
    result = await docx_suggest_edit(
        file_id="d1", find_text="quick", replace_text="slow", author="Claude"
    )
    assert result["file_id"] == "d1"
    assert result["occurrences_edited"] == 1
    mock_drive.files().update.assert_called_once()


@pytest.mark.asyncio
async def test_docx_suggest_edit_errors_on_google_doc(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = {
        "name": "native",
        "mimeType": "application/vnd.google-apps.document",
    }
    from gsuite_mcp.server import docx_suggest_edit
    result = await docx_suggest_edit(
        file_id="x", find_text="a", replace_text="b"
    )
    assert result["error"] == "NOT_A_DOCX"
    assert "replace_text" in result["message"]


@pytest.mark.asyncio
async def test_docx_suggest_edit_find_text_not_found(mock_drive):
    original = make_docx([("Hello world", None)])
    mock_drive.files().get.return_value.execute.return_value = {
        "name": "doc.docx",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "size": str(len(original)),
    }
    mock_drive.files().get_media.return_value.execute.return_value = original

    from gsuite_mcp.server import docx_suggest_edit
    result = await docx_suggest_edit(
        file_id="d1", find_text="xyz", replace_text="abc"
    )
    assert result["error"] == "FIND_TEXT_NOT_FOUND"
