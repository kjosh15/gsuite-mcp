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
async def test_download_binary_file(mock_drive):
    """Download a regular file (not a Google Doc) by file_id."""
    file_bytes = b"hello world"
    mock_drive.files().get.return_value.execute.return_value = {
        "name": "test.docx",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "size": str(len(file_bytes)),
    }
    mock_drive.files().get_media.return_value.execute.return_value = file_bytes

    from gdrive_mcp.server import download_file

    result = await download_file(file_id="abc123")

    assert result["file_id"] == "abc123"
    assert result["file_name"] == "test.docx"
    assert result["content_base64"] == base64.b64encode(file_bytes).decode()


@pytest.mark.asyncio
async def test_download_google_doc_as_docx(mock_drive):
    """Export a Google Doc as .docx."""
    file_bytes = b"exported docx content"
    mock_drive.files().get.return_value.execute.return_value = {
        "name": "My Document",
        "mimeType": "application/vnd.google-apps.document",
        "size": "0",
    }
    mock_drive.files().export.return_value.execute.return_value = file_bytes

    from gdrive_mcp.server import download_file

    export_mime = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    result = await download_file(file_id="doc123", export_format=export_mime)

    assert result["content_base64"] == base64.b64encode(file_bytes).decode()
    assert result["file_name"] == "My Document"
