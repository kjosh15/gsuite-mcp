from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def mock_drive():
    with patch("gdrive_mcp.auth.get_drive_service") as mock:
        service = MagicMock()
        mock.return_value = service
        yield service


@pytest.mark.asyncio
async def test_get_file_metadata(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = {
        "id": "abc123",
        "name": "Stakeholder_Map.docx",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "size": "45231",
        "modifiedTime": "2026-04-01T10:00:00Z",
        "webViewLink": "https://drive.google.com/file/d/abc123/view",
        "parents": ["folder_xyz"],
        "capabilities": {"canEdit": True, "canDownload": True},
    }

    from gdrive_mcp.server import get_file_metadata

    result = await get_file_metadata(file_id="abc123")

    assert result["file_id"] == "abc123"
    assert result["name"] == "Stakeholder_Map.docx"
    assert result["size_bytes"] == 45231
    assert result["capabilities"]["canEdit"] is True
