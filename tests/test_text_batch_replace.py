"""Tests for the text_batch_replace tool wrapper in server.py."""

from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def mock_drive():
    with patch("gsuite_mcp.auth.get_drive_service") as mock:
        drive = MagicMock()
        mock.return_value = drive
        yield drive


def _live_meta(**overrides):
    base = {
        "name": "strategy.md",
        "mimeType": "text/markdown",
        "size": "40",
        "modifiedTime": "2026-07-10T00:00:00Z",
        "md5Checksum": "abc123",
        "trashed": False,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_empty_edits_rejected(mock_drive):
    from gsuite_mcp.server import text_batch_replace
    result = await text_batch_replace("f1", [])
    assert result["error"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_edit_missing_required_field_rejected(mock_drive):
    from gsuite_mcp.server import text_batch_replace
    result = await text_batch_replace("f1", [{"find": "x"}])
    assert result["error"] == "INVALID_INPUT"
    assert "index 0" in result["message"]


@pytest.mark.asyncio
async def test_refuses_trashed(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = _live_meta(
        trashed=True, trashedTime="2026-07-01T00:00:00Z",
    )
    from gsuite_mcp.server import text_batch_replace
    result = await text_batch_replace("f1", [{"find": "a", "replace": "b"}])
    assert result["error"] == "TRASHED_FILE"


@pytest.mark.asyncio
async def test_seven_edit_batch_applies_atomically(mock_drive):
    content = "\n".join(f"line {i}: old{i}" for i in range(7)).encode()
    mock_drive.files().get.return_value.execute.return_value = _live_meta(size=str(len(content)))
    mock_drive.files().get_media.return_value.execute.return_value = content
    mock_drive.files().update.return_value.execute.return_value = {
        "id": "f1", "name": "strategy.md", "webViewLink": "https://x",
        "version": "2", "modifiedTime": "2026-07-10T01:00:00Z",
    }
    call_count = {"n": 0}
    def revisions_execute():
        call_count["n"] += 1
        return {"revisions": [{"id": f"rev{call_count['n']}"}]}
    revisions_mock = MagicMock()
    revisions_mock.execute = revisions_execute
    mock_drive.revisions().list.return_value = revisions_mock

    edits = [
        {"find": f"old{i}", "replace": f"new{i}", "expected_count": 1}
        for i in range(7)
    ]
    from gsuite_mcp.server import text_batch_replace
    result = await text_batch_replace("f1", edits)
    assert "error" not in result
    assert len(result["per_edit"]) == 7
    assert all(e["applied"] for e in result["per_edit"])
    mock_drive.files().update.assert_called_once()


@pytest.mark.asyncio
async def test_one_failing_edit_aborts_whole_batch_no_write(mock_drive):
    content = b"marker0 marker1 marker1 marker2"
    mock_drive.files().get.return_value.execute.return_value = _live_meta(size=str(len(content)))
    mock_drive.files().get_media.return_value.execute.return_value = content

    edits = [
        {"find": "marker0", "replace": "X", "expected_count": 1},
        {"find": "marker1", "replace": "Y", "expected_count": 1},  # 2 matches -> fails
        {"find": "marker2", "replace": "Z", "expected_count": 1},
    ]
    from gsuite_mcp.server import text_batch_replace
    result = await text_batch_replace("f1", edits)
    assert result["error"] == "BATCH_ABORTED"
    assert result["failed_edit_index"] == 1
    mock_drive.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_returns_per_edit_counts_no_write(mock_drive):
    content = b"alpha beta gamma"
    mock_drive.files().get.return_value.execute.return_value = _live_meta(size=str(len(content)))
    mock_drive.files().get_media.return_value.execute.return_value = content

    edits = [
        {"find": "alpha", "replace": "1", "expected_count": 1},
        {"find": "beta", "replace": "2", "expected_count": 1},
    ]
    from gsuite_mcp.server import text_batch_replace
    result = await text_batch_replace("f1", edits, dry_run=True)
    assert result["dry_run"] is True
    assert len(result["per_edit"]) == 2
    mock_drive.files().update.assert_not_called()
