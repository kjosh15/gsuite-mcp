import base64
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def mock_drive():
    with patch("gdrive_mcp.server.get_drive_service") as mock:
        service = MagicMock()
        mock.return_value = service
        yield service


@pytest.mark.asyncio
async def test_upload_new_file(mock_drive):
    """Create a new file in a folder."""
    mock_drive.files().create.return_value.execute.return_value = {
        "id": "new123",
        "name": "report.docx",
        "webViewLink": "https://drive.google.com/file/d/new123/view",
        "version": "1",
        "modifiedTime": "2026-04-08T10:00:00Z",
    }

    from gdrive_mcp.server import upload_file

    content = base64.b64encode(b"file content").decode()
    result = await upload_file(
        content_base64=content,
        file_name="report.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        parent_folder_id="folder456",
    )

    assert result["file_id"] == "new123"
    assert result["file_name"] == "report.docx"
    mock_drive.files().create.assert_called_once()


@pytest.mark.asyncio
async def test_upload_update_existing(mock_drive):
    """Update an existing file in place (preserving file ID)."""
    mock_drive.files().update.return_value.execute.return_value = {
        "id": "existing789",
        "name": "report.docx",
        "webViewLink": "https://drive.google.com/file/d/existing789/view",
        "version": "4",
        "modifiedTime": "2026-04-08T14:30:00Z",
    }

    from gdrive_mcp.server import upload_file

    content = base64.b64encode(b"updated content").decode()
    result = await upload_file(
        content_base64=content,
        file_name="report.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_id="existing789",
    )

    assert result["file_id"] == "existing789"
    assert result["version"] == "4"
    mock_drive.files().update.assert_called_once()
    mock_drive.files().create.assert_not_called()
