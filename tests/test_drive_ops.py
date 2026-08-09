"""Tests for drive_ops — trashed-file metadata and trash/untrash operations."""

import base64
import pytest
from unittest.mock import MagicMock

from gsuite_mcp import drive_ops


def _mock_drive_service(meta_response):
    svc = MagicMock()
    svc.files().get.return_value.execute.return_value = meta_response
    return svc


# -------------------------------------------------------------------
# get_file_metadata — trashed field
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_file_metadata_trashed_true():
    svc = _mock_drive_service({
        "id": "f1", "name": "Trashed Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-05-19T00:00:00Z", "trashed": True,
        "trashedTime": "2026-05-06T18:00:00Z",
        "webViewLink": "https://...", "parents": [], "capabilities": {},
    })
    result = await drive_ops.get_file_metadata(svc, "f1")
    assert result["trashed"] is True
    assert result["trashed_time"] == "2026-05-06T18:00:00Z"


@pytest.mark.asyncio
async def test_get_file_metadata_trashed_false():
    svc = _mock_drive_service({
        "id": "f1", "name": "Live Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-05-19T00:00:00Z", "trashed": False,
        "webViewLink": "https://...", "parents": [], "capabilities": {},
    })
    result = await drive_ops.get_file_metadata(svc, "f1")
    assert result["trashed"] is False
    assert result.get("trashed_time") is None


@pytest.mark.asyncio
async def test_get_file_metadata_trashed_absent():
    """When API doesn't return trashed field, default to False."""
    svc = _mock_drive_service({
        "id": "f1", "name": "Old Doc", "mimeType": "text/plain",
        "modifiedTime": "2026-05-19T00:00:00Z",
        "webViewLink": "https://...", "parents": [], "capabilities": {},
    })
    result = await drive_ops.get_file_metadata(svc, "f1")
    assert result["trashed"] is False


# -------------------------------------------------------------------
# search_files — trashed field
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_files_includes_trashed_field():
    svc = MagicMock()
    svc.files().list.return_value.execute.return_value = {
        "files": [
            {"id": "f1", "name": "Live", "mimeType": "text/plain",
             "modifiedTime": "2026-05-19T00:00:00Z", "trashed": False,
             "webViewLink": "https://...", "parents": []},
            {"id": "f2", "name": "Dead", "mimeType": "text/plain",
             "modifiedTime": "2026-05-19T00:00:00Z", "trashed": True,
             "trashedTime": "2026-05-10T00:00:00Z",
             "webViewLink": "https://...", "parents": []},
        ]
    }
    result = await drive_ops.search_files(svc, "name contains 'test'")
    assert result["files"][0]["trashed"] is False
    assert result["files"][1]["trashed"] is True
    assert result["files"][1]["trashed_time"] == "2026-05-10T00:00:00Z"


# -------------------------------------------------------------------
# search_files — empty vs unresolved status (D8)
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_files_returns_results_status_when_files_found():
    svc = MagicMock()
    svc.files().list.return_value.execute.return_value = {
        "files": [{"id": "f1", "name": "a.txt", "mimeType": "text/plain",
                   "modifiedTime": "2026-08-01T00:00:00Z",
                   "webViewLink": "https://x", "parents": []}]
    }
    result = await drive_ops.search_files(svc, "name = 'a.txt'")
    assert result["status"] == "results"


@pytest.mark.asyncio
async def test_search_files_returns_empty_status_for_genuinely_empty_query():
    svc = MagicMock()
    svc.files().list.return_value.execute.return_value = {"files": []}
    result = await drive_ops.search_files(svc, "name = 'nothing_matches_this'")
    assert result["status"] == "empty"
    assert "unresolved_reference" not in result


@pytest.mark.asyncio
async def test_search_files_returns_unresolved_status_for_missing_parent_folder():
    from googleapiclient.errors import HttpError

    svc = MagicMock()
    svc.files().list.return_value.execute.return_value = {"files": []}
    response = MagicMock()
    response.status = 404
    svc.files().get.return_value.execute.side_effect = HttpError(
        response, b"not found"
    )

    result = await drive_ops.search_files(svc, "'bad_folder_id' in parents")
    assert result["status"] == "unresolved"
    assert result["unresolved_reference"] == "bad_folder_id"


@pytest.mark.asyncio
async def test_search_files_returns_empty_status_when_parent_folder_exists_but_empty():
    svc = MagicMock()
    svc.files().list.return_value.execute.return_value = {"files": []}
    svc.files().get.return_value.execute.return_value = {"id": "real_folder"}

    result = await drive_ops.search_files(svc, "'real_folder' in parents")
    assert result["status"] == "empty"
    assert "unresolved_reference" not in result


# -------------------------------------------------------------------
# download_file — trashed field
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_file_includes_trashed_field():
    svc = MagicMock()
    svc.files().get.return_value.execute.return_value = {
        "name": "test.txt", "mimeType": "text/plain", "size": "5",
        "trashed": True, "trashedTime": "2026-05-06T00:00:00Z",
    }
    svc.files().get_media.return_value.execute.return_value = b"hello"
    result = await drive_ops.download_file(svc, "f1")
    assert result["trashed"] is True
    assert result["trashed_time"] == "2026-05-06T00:00:00Z"


# -------------------------------------------------------------------
# trash_file / untrash_file
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trash_file():
    svc = MagicMock()
    svc.files().update.return_value.execute.return_value = {
        "id": "f1", "trashed": True, "trashedTime": "2026-05-19T14:30:00Z",
    }
    result = await drive_ops.trash_file(svc, "f1")
    assert result["file_id"] == "f1"
    assert result["trashed"] is True
    assert result["trashed_time"] == "2026-05-19T14:30:00Z"
    call_args = svc.files().update.call_args
    assert call_args.kwargs["body"] == {"trashed": True}


@pytest.mark.asyncio
async def test_untrash_file():
    svc = MagicMock()
    svc.files().update.return_value.execute.return_value = {
        "id": "f1", "trashed": False,
    }
    result = await drive_ops.untrash_file(svc, "f1")
    assert result["file_id"] == "f1"
    assert result["trashed"] is False
    call_args = svc.files().update.call_args
    assert call_args.kwargs["body"] == {"trashed": False}


# -------------------------------------------------------------------
# update_file_metadata
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_file_metadata_rename_only():
    svc = MagicMock()
    svc.files().update.return_value.execute.return_value = {
        "id": "f1", "name": "New Name.txt", "parents": ["folder_a"],
        "mimeType": "text/plain", "modifiedTime": "2026-08-01T00:00:00Z",
    }
    result = await drive_ops.update_file_metadata(svc, "f1", name="New Name.txt")
    assert result["file_id"] == "f1"
    assert result["name"] == "New Name.txt"
    assert result["parents"] == ["folder_a"]
    call_kwargs = svc.files().update.call_args.kwargs
    assert call_kwargs["body"] == {"name": "New Name.txt"}
    assert "addParents" not in call_kwargs
    assert "removeParents" not in call_kwargs
    assert call_kwargs["supportsAllDrives"] is True
    assert call_kwargs["fileId"] == "f1"


@pytest.mark.asyncio
async def test_update_file_metadata_move_only():
    svc = MagicMock()
    svc.files().update.return_value.execute.return_value = {
        "id": "f1", "name": "Doc", "parents": ["folder_b"],
        "mimeType": "text/plain", "modifiedTime": "2026-08-01T00:00:00Z",
    }
    result = await drive_ops.update_file_metadata(
        svc, "f1", add_parent_id="folder_b", remove_parent_id="folder_a"
    )
    assert result["parents"] == ["folder_b"]
    call_kwargs = svc.files().update.call_args.kwargs
    assert "body" not in call_kwargs
    assert call_kwargs["addParents"] == "folder_b"
    assert call_kwargs["removeParents"] == "folder_a"


@pytest.mark.asyncio
async def test_update_file_metadata_rename_and_move():
    svc = MagicMock()
    svc.files().update.return_value.execute.return_value = {
        "id": "f1", "name": "New Name", "parents": ["folder_b"],
        "mimeType": "text/plain", "modifiedTime": "2026-08-01T00:00:00Z",
    }
    result = await drive_ops.update_file_metadata(
        svc, "f1", name="New Name",
        add_parent_id="folder_b", remove_parent_id="folder_a",
    )
    call_kwargs = svc.files().update.call_args.kwargs
    assert call_kwargs["body"] == {"name": "New Name"}
    assert call_kwargs["addParents"] == "folder_b"
    assert call_kwargs["removeParents"] == "folder_a"
    assert result["file_id"] == "f1"
    assert result["name"] == "New Name"
    assert result["parents"] == ["folder_b"]


@pytest.mark.asyncio
async def test_update_file_metadata_round_trip_preserves_file_id():
    """Rename, move, then reverse both — file ID must never change."""
    svc = MagicMock()

    def resp(name, parents):
        return {
            "id": "f1", "name": name, "parents": parents,
            "mimeType": "text/plain", "modifiedTime": "2026-08-01T00:00:00Z",
        }

    svc.files().update.return_value.execute.side_effect = [
        resp("Renamed.txt", ["folder_a"]),
        resp("Renamed.txt", ["folder_b"]),
        resp("Original.txt", ["folder_b"]),
        resp("Original.txt", ["folder_a"]),
    ]
    r1 = await drive_ops.update_file_metadata(svc, "f1", name="Renamed.txt")
    r2 = await drive_ops.update_file_metadata(
        svc, "f1", add_parent_id="folder_b", remove_parent_id="folder_a"
    )
    r3 = await drive_ops.update_file_metadata(svc, "f1", name="Original.txt")
    r4 = await drive_ops.update_file_metadata(
        svc, "f1", add_parent_id="folder_a", remove_parent_id="folder_b"
    )
    for r in (r1, r2, r3, r4):
        assert r["file_id"] == "f1"


# -------------------------------------------------------------------
# download_file_bytes
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_file_bytes_returns_raw_bytes():
    svc = MagicMock()
    svc.files().get_media.return_value.execute.return_value = b"raw file content"
    result = await drive_ops.download_file_bytes(svc, "f1")
    assert result == b"raw file content"
    svc.files().get_media.assert_called_with(fileId="f1")


# -------------------------------------------------------------------
# get_file_metadata — size_unavailable, md5_checksum
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_file_metadata_native_doc_size_unavailable():
    svc = _mock_drive_service({
        "id": "f1", "name": "Decision_Log",
        "mimeType": "application/vnd.google-apps.document",
        "size": "64707",  # Drive reports a number here, but it's storage
                           # quota usage, not the exported byte count — wrong.
        "modifiedTime": "2026-05-19T00:00:00Z",
        "webViewLink": "https://...", "parents": [], "capabilities": {},
    })
    result = await drive_ops.get_file_metadata(svc, "f1")
    assert result["size_bytes"] is None
    assert result["size_unavailable"] is True


@pytest.mark.asyncio
async def test_get_file_metadata_plain_file_size_accurate():
    svc = _mock_drive_service({
        "id": "f1", "name": "notes.md", "mimeType": "text/markdown",
        "size": "32285",
        "modifiedTime": "2026-05-19T00:00:00Z",
        "webViewLink": "https://...", "parents": [], "capabilities": {},
    })
    result = await drive_ops.get_file_metadata(svc, "f1")
    assert result["size_bytes"] == 32285
    assert "size_unavailable" not in result


@pytest.mark.asyncio
async def test_get_file_metadata_exposes_md5_checksum_when_present():
    svc = _mock_drive_service({
        "id": "f1", "name": "notes.md", "mimeType": "text/markdown",
        "size": "20", "md5Checksum": "d41d8cd98f00b204e9800998ecf8427e",
        "modifiedTime": "2026-05-19T00:00:00Z",
        "webViewLink": "https://...", "parents": [], "capabilities": {},
    })
    result = await drive_ops.get_file_metadata(svc, "f1")
    assert result["md5_checksum"] == "d41d8cd98f00b204e9800998ecf8427e"


@pytest.mark.asyncio
async def test_get_file_metadata_omits_md5_checksum_for_native_doc():
    svc = _mock_drive_service({
        "id": "f1", "name": "Decision_Log",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-05-19T00:00:00Z",
        "webViewLink": "https://...", "parents": [], "capabilities": {},
    })
    result = await drive_ops.get_file_metadata(svc, "f1")
    assert "md5_checksum" not in result


# -------------------------------------------------------------------
# upload_file_from_path
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_file_from_path_creates_new_file(tmp_path):
    svc = MagicMock()
    svc.files().create.return_value.execute.return_value = {
        "id": "new1", "name": "archive.md", "webViewLink": "https://x",
        "version": "1", "modifiedTime": "2026-08-05T00:00:00Z",
    }
    svc.files().get.return_value.execute.return_value = {"size": "11"}

    local_file = tmp_path / "archive.md"
    local_file.write_bytes(b"hello world")

    result = await drive_ops.upload_file_from_path(
        svc, str(local_file), file_name="archive.md", mime_type="text/markdown",
    )
    assert result["file_id"] == "new1"
    assert result["bytes_uploaded"] == 11
    assert result["file_size"] == 11
    svc.files().create.assert_called_once()
    svc.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_upload_file_from_path_updates_existing_file(tmp_path):
    svc = MagicMock()
    svc.files().update.return_value.execute.return_value = {
        "id": "f1", "name": "archive.md", "webViewLink": "https://x",
        "version": "2", "modifiedTime": "2026-08-05T00:00:00Z",
    }
    svc.files().get.return_value.execute.return_value = {"size": "11"}

    local_file = tmp_path / "archive.md"
    local_file.write_bytes(b"hello world")

    result = await drive_ops.upload_file_from_path(
        svc, str(local_file), file_name="archive.md", mime_type="text/markdown",
        file_id="f1",
    )
    assert result["file_id"] == "f1"
    svc.files().update.assert_called_once()
    svc.files().create.assert_not_called()


# -------------------------------------------------------------------
# upload_file — expected_bytes / expected_sha256 (D10)
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_file_rejects_expected_bytes_mismatch():
    svc = MagicMock()
    content_b64 = base64.b64encode(b"hello world").decode()  # 11 bytes
    result = await drive_ops.upload_file(
        svc, content_b64, "f.txt", "text/plain", expected_bytes=999,
    )
    assert result["error"] == "PAYLOAD_SIZE_MISMATCH"
    assert result["actual_bytes"] == 11
    assert result["expected_bytes"] == 999
    svc.files().create.assert_not_called()
    svc.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_upload_file_rejects_expected_sha256_mismatch():
    svc = MagicMock()
    content_b64 = base64.b64encode(b"hello world").decode()
    result = await drive_ops.upload_file(
        svc, content_b64, "f.txt", "text/plain", expected_sha256="0" * 64,
    )
    assert result["error"] == "PAYLOAD_HASH_MISMATCH"
    svc.files().create.assert_not_called()


@pytest.mark.asyncio
async def test_upload_file_accepts_matching_expected_bytes_and_sha256():
    import hashlib
    content = b"hello world"
    svc = MagicMock()
    svc.files().create.return_value.execute.return_value = {
        "id": "f1", "name": "f.txt", "webViewLink": "https://x",
        "version": "1", "modifiedTime": "2026-08-05T00:00:00Z",
    }
    svc.files().get.return_value.execute.return_value = {"size": "11"}
    result = await drive_ops.upload_file(
        svc, base64.b64encode(content).decode(), "f.txt", "text/plain",
        expected_bytes=11, expected_sha256=hashlib.sha256(content).hexdigest(),
    )
    assert "error" not in result
    assert result["file_id"] == "f1"
