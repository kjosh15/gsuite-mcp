"""Tests for upload_file_start/upload_file_chunk/upload_file_finish (D9b)."""

import base64
from unittest.mock import patch, MagicMock

import pytest

from gsuite_mcp import upload_session


@pytest.fixture(autouse=True)
def _clear_sessions():
    yield
    for uid in list(upload_session._SESSIONS.keys()):
        upload_session.cleanup(uid)


@pytest.fixture
def mock_drive():
    with patch("gsuite_mcp.auth.get_drive_service") as mock:
        service = MagicMock()
        mock.return_value = service
        yield service


@pytest.mark.asyncio
async def test_upload_file_start_returns_upload_id(mock_drive):
    from gsuite_mcp.server import upload_file_start
    result = await upload_file_start(
        file_name="archive.md", mime_type="text/markdown", total_bytes=11,
    )
    assert "upload_id" in result
    assert "error" not in result


@pytest.mark.asyncio
async def test_upload_file_start_rejects_non_positive_total_bytes(mock_drive):
    from gsuite_mcp.server import upload_file_start
    result = await upload_file_start(
        file_name="x", mime_type="text/plain", total_bytes=0,
    )
    assert result["error"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_upload_file_start_refuses_trashed_file_id(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = {
        "name": "x", "trashed": True, "trashedTime": "2026-08-05T00:00:00Z",
    }
    from gsuite_mcp.server import upload_file_start
    result = await upload_file_start(
        file_name="x", mime_type="text/plain", total_bytes=5, file_id="f1",
    )
    assert result["error"] == "TRASHED_FILE"


@pytest.mark.asyncio
async def test_upload_file_chunk_unknown_id_returns_upload_not_found(mock_drive):
    from gsuite_mcp.server import upload_file_chunk
    result = await upload_file_chunk(
        upload_id="nonexistent", chunk_base64=base64.b64encode(b"x").decode(),
        chunk_index=0,
    )
    assert result["error"] == "UPLOAD_NOT_FOUND"


@pytest.mark.asyncio
async def test_upload_file_finish_end_to_end(mock_drive):
    mock_drive.files().create.return_value.execute.return_value = {
        "id": "new1", "name": "archive.md", "webViewLink": "https://x",
        "version": "1", "modifiedTime": "2026-08-05T00:00:00Z",
    }
    mock_drive.files().get.return_value.execute.return_value = {"size": "11"}

    from gsuite_mcp.server import upload_file_start, upload_file_chunk, upload_file_finish

    start_result = await upload_file_start(
        file_name="archive.md", mime_type="text/markdown", total_bytes=11,
    )
    upload_id = start_result["upload_id"]

    r1 = await upload_file_chunk(
        upload_id=upload_id, chunk_base64=base64.b64encode(b"hello ").decode(),
        chunk_index=0,
    )
    assert r1["bytes_received"] == 6
    r2 = await upload_file_chunk(
        upload_id=upload_id, chunk_base64=base64.b64encode(b"world").decode(),
        chunk_index=1,
    )
    assert r2["bytes_received"] == 11

    finish_result = await upload_file_finish(upload_id=upload_id)
    assert finish_result["file_id"] == "new1"
    assert finish_result["bytes_uploaded"] == 11

    # session is cleaned up after finish
    assert upload_session.get_session(upload_id) is None


@pytest.mark.asyncio
async def test_upload_file_finish_incomplete_upload_errors(mock_drive):
    from gsuite_mcp.server import upload_file_start, upload_file_chunk, upload_file_finish

    start_result = await upload_file_start(
        file_name="x", mime_type="text/plain", total_bytes=11,
    )
    upload_id = start_result["upload_id"]
    await upload_file_chunk(
        upload_id=upload_id, chunk_base64=base64.b64encode(b"short").decode(),
        chunk_index=0,
    )
    result = await upload_file_finish(upload_id=upload_id)
    assert result["error"] == "INCOMPLETE_OR_CORRUPT_UPLOAD"

    # session (and its temp file) is cleaned up even on this failure path
    assert upload_session.get_session(upload_id) is None


@pytest.mark.asyncio
async def test_upload_file_finish_unknown_id_returns_upload_not_found(mock_drive):
    from gsuite_mcp.server import upload_file_finish
    result = await upload_file_finish(upload_id="nonexistent")
    assert result["error"] == "UPLOAD_NOT_FOUND"
