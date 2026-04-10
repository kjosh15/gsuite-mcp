from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def mock_services():
    """Mock drive, docs, and sheets services."""
    with patch("gdrive_mcp.auth.get_drive_service") as mock_drive, \
         patch("gdrive_mcp.auth.get_docs_service") as mock_docs, \
         patch("gdrive_mcp.auth.get_sheets_service") as mock_sheets:
        drive = MagicMock()
        docs = MagicMock()
        sheets = MagicMock()
        mock_drive.return_value = drive
        mock_docs.return_value = docs
        mock_sheets.return_value = sheets
        yield {"drive": drive, "docs": docs, "sheets": sheets}


@pytest.mark.asyncio
async def test_append_to_google_doc_uses_docs_api(mock_services):
    """Appending to a Google Doc uses Docs API batchUpdate with InsertTextRequest."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    # files.get returns a Google Doc
    drive.files().get.return_value.execute.return_value = {
        "name": "Index",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }
    # documents.get returns a doc body with endIndex
    docs.documents().get.return_value.execute.return_value = {
        "body": {
            "content": [
                {"endIndex": 1},
                {"endIndex": 42},
            ]
        }
    }
    docs.documents().batchUpdate.return_value.execute.return_value = {}

    from gdrive_mcp.server import append_to_file
    result = await append_to_file(
        file_id="doc123", content="new line", separator="\n"
    )

    assert result["mode"] == "docs_native"
    assert result["file_id"] == "doc123"
    assert result["bytes_appended"] > 0

    # verify batchUpdate was called with InsertTextRequest
    call_args = docs.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    assert len(requests) == 1
    assert "insertText" in requests[0]
    assert requests[0]["insertText"]["text"] == "\nnew line"
