from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def mock_drive():
    with patch("gdrive_mcp.server.get_drive_service") as mock:
        service = MagicMock()
        mock.return_value = service
        yield service


@pytest.mark.asyncio
async def test_search_files(mock_drive):
    mock_drive.files().list.return_value.execute.return_value = {
        "files": [
            {
                "id": "f1",
                "name": "Stakeholder_Map.docx",
                "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "modifiedTime": "2026-04-01T10:00:00Z",
                "webViewLink": "https://drive.google.com/file/d/f1/view",
                "parents": ["folder1"],
            }
        ]
    }

    from gdrive_mcp.server import search_files

    result = await search_files(query="name contains 'Stakeholder'")

    assert len(result["files"]) == 1
    assert result["files"][0]["file_id"] == "f1"
    assert result["files"][0]["name"] == "Stakeholder_Map.docx"
