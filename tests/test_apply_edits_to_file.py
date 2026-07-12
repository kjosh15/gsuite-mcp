"""Tests for text_ops.apply_edits_to_file — the core read-match-guard-write flow."""

import base64
from unittest.mock import MagicMock

import pytest

from gsuite_mcp import text_ops


def _meta(**overrides):
    base = {
        "name": "notes.md",
        "mimeType": "text/markdown",
        "size": "20",
        "modifiedTime": "2026-07-10T00:00:00Z",
        "md5Checksum": "abc123",
    }
    base.update(overrides)
    return base


def _mock_drive(initial_content: bytes, *, concurrency_meta: dict | None = None):
    """A MagicMock drive service wired for the happy path.

    files().get_media() -> initial_content
    files().get()       -> a superset dict serving both the backup helper's
                            name/parents lookup and the concurrency recheck's
                            modifiedTime/md5Checksum lookup
    files().update()    -> upload_file's write path
    files().copy()      -> backup helper
    revisions().list()  -> called twice (before/after); returns growing ids
    """
    svc = MagicMock()
    svc.files().get_media.return_value.execute.return_value = initial_content

    get_return = {
        "name": "notes.md",
        "parents": ["folder1"],
        "modifiedTime": "2026-07-10T00:00:00Z",
        "md5Checksum": "abc123",
        "size": "999",
    }
    if concurrency_meta is not None:
        get_return.update(concurrency_meta)
    svc.files().get.return_value.execute.return_value = get_return

    svc.files().update.return_value.execute.return_value = {
        "id": "f1", "name": "notes.md", "webViewLink": "https://x",
        "version": "2", "modifiedTime": "2026-07-10T01:00:00Z",
    }
    svc.files().copy.return_value.execute.return_value = {
        "id": "backup1", "name": "notes.md__autobackup_2026-07-10T00:00:00Z",
    }

    call_count = {"n": 0}
    def revisions_execute():
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"revisions": [{"id": "rev_before"}]}
        return {"revisions": [{"id": "rev_before"}, {"id": "rev_after"}]}
    revisions_mock = MagicMock()
    revisions_mock.execute = revisions_execute
    svc.revisions().list.return_value = revisions_mock

    return svc


@pytest.mark.asyncio
async def test_unsupported_mime_refused_before_download():
    svc = _mock_drive(b"content")
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(mimeType="application/vnd.google-apps.document"),
        edits=[{"find": "x", "replace": "y"}],
    )
    assert result["error"] == "UNSUPPORTED_MIME"
    assert "replace_text" in result["message"]
    svc.files().get_media.assert_not_called()


@pytest.mark.asyncio
async def test_file_too_large_refused():
    svc = _mock_drive(b"content")
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(size=str(text_ops.MAX_FILE_SIZE_BYTES + 1)),
        edits=[{"find": "x", "replace": "y"}],
    )
    assert result["error"] == "FILE_TOO_LARGE"
    svc.files().get_media.assert_not_called()


@pytest.mark.asyncio
async def test_not_text_file_refused():
    svc = _mock_drive(b"\xff\xfe\x00\x01invalid")
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(), edits=[{"find": "x", "replace": "y"}],
    )
    assert result["error"] == "NOT_TEXT_FILE"
    svc.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_count_mismatch_no_write():
    svc = _mock_drive(b"foo foo")
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(),
        edits=[{"find": "foo", "replace": "bar", "expected_count": 1}],
    )
    assert result["error"] == "COUNT_MISMATCH"
    assert result["matches_found"] == 2
    svc.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_no_match_error_code():
    svc = _mock_drive(b"hello world")
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(),
        edits=[{"find": "xyz", "replace": "abc", "expected_count": 1}],
    )
    assert result["error"] == "NO_MATCH"


@pytest.mark.asyncio
async def test_batch_abort_names_failing_edit_no_write():
    svc = _mock_drive(b"marker0 marker1 marker1")
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(),
        edits=[
            {"find": "marker0", "replace": "X", "expected_count": 1},
            {"find": "marker1", "replace": "Y", "expected_count": 1},
        ],
    )
    assert result["error"] == "BATCH_ABORTED"
    assert result["failed_edit_index"] == 1
    svc.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_writes_nothing():
    svc = _mock_drive(b"hello world")
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(),
        edits=[{"find": "world", "replace": "there", "expected_count": 1}],
        dry_run=True,
    )
    assert result["dry_run"] is True
    assert result["matches_found"] == 1
    svc.files().update.assert_not_called()
    svc.files().copy.assert_not_called()


@pytest.mark.asyncio
async def test_blast_radius_trips_without_confirm():
    big_find = "x" * 500
    content = big_find + " tail"
    svc = _mock_drive(content.encode())
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(),
        edits=[{"find": big_find, "replace": "", "expected_count": 1}],
    )
    assert result["error"] == "BLAST_RADIUS_EXCEEDED"
    svc.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_confirmed_blast_radius_creates_backup_and_writes():
    big_find = "x" * 500
    content = big_find + " tail"
    svc = _mock_drive(content.encode())
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(),
        edits=[{"find": big_find, "replace": "", "expected_count": 1}],
        confirm_delete_chars=500,
    )
    assert "error" not in result
    assert result["backup_file_id"] == "backup1"
    svc.files().update.assert_called_once()


@pytest.mark.asyncio
async def test_concurrent_modification_detected_no_write():
    svc = _mock_drive(
        b"hello world",
        concurrency_meta={"modifiedTime": "2026-07-10T02:00:00Z", "md5Checksum": "changed"},
    )
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(),
        edits=[{"find": "world", "replace": "there", "expected_count": 1}],
    )
    assert result["error"] == "CONCURRENT_MODIFICATION"
    svc.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_successful_edit_returns_revision_ids_and_diff_counts():
    content = b"Status: old\r\nOwner: josh\r\n"
    svc = _mock_drive(content)
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(),
        edits=[{"find": "Status: old", "replace": "Status: new", "expected_count": 1}],
    )
    assert "error" not in result
    assert result["matches_found"] == 1
    assert result["revision_id_before"] == "rev_before"
    assert result["revision_id_after"] == "rev_after"
    assert result["chars_deleted"] == len("Status: old")
    assert result["chars_inserted"] == len("Status: new")
    svc.files().update.assert_called_once()


@pytest.mark.asyncio
async def test_successful_edit_preserves_crlf(monkeypatch):
    content = b"Status: old\r\nOwner: josh\r\n"
    svc = _mock_drive(content)

    captured = {}
    async def fake_upload_file(service, content_base64, file_name, mime_type, file_id=None, parent_folder_id=None):
        captured["bytes"] = base64.b64decode(content_base64)
        return {"file_id": file_id, "file_name": file_name, "modified_time": "2026-07-10T01:00:00Z",
                "bytes_uploaded": len(captured["bytes"]), "file_size": len(captured["bytes"])}

    monkeypatch.setattr(text_ops.drive_ops, "upload_file", fake_upload_file)

    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(),
        edits=[{"find": "Status: old", "replace": "Status: new", "expected_count": 1}],
    )
    assert "error" not in result
    assert captured["bytes"] == b"Status: new\r\nOwner: josh\r\n"


@pytest.mark.asyncio
async def test_successful_batch_edit_applies_all_pairs():
    svc = _mock_drive(b"one two three")
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(),
        edits=[
            {"find": "one", "replace": "1", "expected_count": 1},
            {"find": "two", "replace": "2", "expected_count": 1},
            {"find": "three", "replace": "3", "expected_count": 1},
        ],
    )
    assert "error" not in result
    assert len(result["per_edit"]) == 3
    assert all(e["applied"] for e in result["per_edit"])
