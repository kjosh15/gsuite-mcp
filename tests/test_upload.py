import base64
from unittest.mock import patch, MagicMock

import pytest
from googleapiclient.errors import HttpError


@pytest.fixture
def mock_drive():
    with patch("gsuite_mcp.auth.get_drive_service") as mock:
        service = MagicMock()
        mock.return_value = service
        # Default metadata for trashed check (live file)
        service.files().get.return_value.execute.return_value = {
            "name": "Test File", "trashed": False,
        }
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
    mock_drive.files().get.return_value.execute.return_value = {
        "size": "12",
    }

    from gsuite_mcp.server import upload_file

    content = base64.b64encode(b"file content").decode()
    result = await upload_file(
        content_base64=content,
        file_name="report.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        parent_folder_id="folder456",
    )

    assert result["file_id"] == "new123"
    assert result["file_name"] == "report.docx"
    assert result["bytes_uploaded"] == 12
    assert result["file_size"] == 12
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
    mock_drive.files().get.return_value.execute.return_value = {
        "size": "15",
    }

    from gsuite_mcp.server import upload_file

    content = base64.b64encode(b"updated content").decode()
    result = await upload_file(
        content_base64=content,
        file_name="report.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_id="existing789",
    )

    assert result["file_id"] == "existing789"
    assert result["version"] == "4"
    assert result["bytes_uploaded"] == 15
    assert result["file_size"] == 15
    mock_drive.files().update.assert_called_once()
    mock_drive.files().create.assert_not_called()


@pytest.mark.asyncio
async def test_upload_returns_size_mismatch_on_truncation(mock_drive):
    """bytes_uploaded vs file_size lets callers detect truncation."""
    mock_drive.files().update.return_value.execute.return_value = {
        "id": "trunc1",
        "name": "big.pdf",
        "webViewLink": "https://drive.google.com/file/d/trunc1/view",
        "version": "2",
        "modifiedTime": "2026-05-06T00:00:00Z",
    }
    # Simulate Drive reporting a smaller file than what we uploaded
    mock_drive.files().get.return_value.execute.return_value = {
        "size": "500",
    }

    from gsuite_mcp.server import upload_file

    payload = b"x" * 1000
    content = base64.b64encode(payload).decode()
    result = await upload_file(
        content_base64=content,
        file_name="big.pdf",
        mime_type="application/pdf",
        file_id="trunc1",
    )

    assert result["bytes_uploaded"] == 1000
    assert result["file_size"] == 500
    # Caller can detect: bytes_uploaded != file_size → truncation
    assert result["bytes_uploaded"] != result["file_size"]


@pytest.mark.asyncio
async def test_upload_native_google_format_no_size_field(mock_drive):
    """Native Google formats don't report size — fallback to bytes_uploaded."""
    mock_drive.files().create.return_value.execute.return_value = {
        "id": "gdoc1",
        "name": "doc",
        "webViewLink": "https://docs.google.com/document/d/gdoc1/edit",
        "version": "1",
        "modifiedTime": "2026-05-06T00:00:00Z",
    }
    # Native Google Docs don't have a 'size' field
    mock_drive.files().get.return_value.execute.return_value = {}

    from gsuite_mcp.server import upload_file

    content = base64.b64encode(b"hello").decode()
    result = await upload_file(
        content_base64=content,
        file_name="doc",
        mime_type="text/plain",
    )

    assert result["bytes_uploaded"] == 5
    assert result["file_size"] == 5  # falls back to bytes_uploaded


@pytest.mark.asyncio
async def test_upload_update_still_raises_unknown_errors(mock_drive):
    """Non-quota errors must still propagate so we don't swallow real bugs."""
    resp = MagicMock()
    resp.status = 500
    resp.reason = "Internal Server Error"
    mock_drive.files().update.return_value.execute.side_effect = HttpError(
        resp=resp, content=b'{"error": {"message": "boom"}}'
    )

    from gsuite_mcp.server import upload_file

    content = base64.b64encode(b"x").decode()
    with pytest.raises(HttpError):
        await upload_file(
            content_base64=content,
            file_name="report.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            file_id="existing789",
        )
