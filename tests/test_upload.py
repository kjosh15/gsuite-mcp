import base64
from unittest.mock import patch, MagicMock

import pytest
from googleapiclient.errors import HttpError


@pytest.fixture
def mock_drive():
    with patch("gdrive_mcp.server.get_drive_service") as mock:
        service = MagicMock()
        mock.return_value = service
        yield service


def _quota_exceeded_error() -> HttpError:
    """Build a realistic storageQuotaExceeded HttpError like the Drive API returns."""
    resp = MagicMock()
    resp.status = 403
    resp.reason = "Forbidden"
    content = (
        b'{"error": {"errors": [{"domain": "usageLimits", '
        b'"reason": "storageQuotaExceeded", "message": '
        b'"Service Accounts do not have storage quota. '
        b'Leverage shared drives instead."}], "code": 403, '
        b'"message": "Service Accounts do not have storage quota."}}'
    )
    return HttpError(resp=resp, content=content)


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


@pytest.mark.asyncio
async def test_upload_create_on_personal_drive_returns_terminal_error(mock_drive):
    """When create() fails with storageQuotaExceeded, return a structured,
    non-retryable error instead of raising. The error must tell the LLM to STOP."""
    mock_drive.files().create.return_value.execute.side_effect = (
        _quota_exceeded_error()
    )

    from gdrive_mcp.server import upload_file

    content = base64.b64encode(b"file content").decode()
    result = await upload_file(
        content_base64=content,
        file_name="report.html",
        mime_type="text/html",
        parent_folder_id="folder456",
    )

    assert result["error"] == "STORAGE_QUOTA_UNSUPPORTED"
    assert result["retryable"] is False
    assert "DO NOT RETRY" in result["message"]
    assert "upload" in result["message"].lower()


@pytest.mark.asyncio
async def test_upload_update_still_raises_unknown_errors(mock_drive):
    """Non-quota errors must still propagate so we don't swallow real bugs."""
    resp = MagicMock()
    resp.status = 500
    resp.reason = "Internal Server Error"
    mock_drive.files().update.return_value.execute.side_effect = HttpError(
        resp=resp, content=b'{"error": {"message": "boom"}}'
    )

    from gdrive_mcp.server import upload_file

    content = base64.b64encode(b"x").decode()
    with pytest.raises(HttpError):
        await upload_file(
            content_base64=content,
            file_name="report.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            file_id="existing789",
        )
