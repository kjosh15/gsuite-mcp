"""Tests for the update_file_metadata tool — rename/move guardrails."""

from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def mock_drive():
    with patch("gsuite_mcp.auth.get_drive_service") as mock:
        service = MagicMock()
        mock.return_value = service
        yield service


@pytest.fixture
def mock_docs():
    with patch("gsuite_mcp.auth.get_docs_service") as mock:
        yield mock


def _live_meta(name="Old Name.txt", parents=None, can_rename=True, can_move=True):
    return {
        "name": name,
        "parents": parents if parents is not None else ["folder_a"],
        "capabilities": {
            "canRename": can_rename,
            "canMoveItemWithinDrive": can_move,
        },
        "trashed": False,
    }


@pytest.mark.asyncio
async def test_update_file_metadata_no_op_returns_no_changes_requested():
    with patch("gsuite_mcp.auth.get_drive_service") as mock_get_drive:
        from gsuite_mcp.server import update_file_metadata
        result = await update_file_metadata("f1")
    assert result["error"] == "NO_CHANGES_REQUESTED"
    mock_get_drive.assert_not_called()


@pytest.mark.asyncio
async def test_update_file_metadata_refuses_trashed(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = {
        "name": "Trashed Doc",
        "parents": ["folder_a"],
        "capabilities": {"canRename": True, "canMoveItemWithinDrive": True},
        "trashed": True,
        "trashedTime": "2026-05-06T18:00:00Z",
    }
    from gsuite_mcp.server import update_file_metadata
    result = await update_file_metadata("f1", name="New Name")
    assert result["error"] == "TRASHED_FILE"
    mock_drive.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_update_file_metadata_not_a_parent(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = _live_meta(
        parents=["folder_a"]
    )
    from gsuite_mcp.server import update_file_metadata
    result = await update_file_metadata("f1", remove_parent_id="folder_zzz")
    assert result["error"] == "NOT_A_PARENT"
    assert result["parents"] == ["folder_a"]
    mock_drive.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_update_file_metadata_cannot_rename(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = _live_meta(
        can_rename=False
    )
    from gsuite_mcp.server import update_file_metadata
    result = await update_file_metadata("f1", name="New Name")
    assert result["error"] == "CANNOT_RENAME"
    mock_drive.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_update_file_metadata_cannot_move(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = _live_meta(
        can_move=False
    )
    from gsuite_mcp.server import update_file_metadata
    result = await update_file_metadata(
        "f1", add_parent_id="folder_b", remove_parent_id="folder_a"
    )
    assert result["error"] == "CANNOT_MOVE"
    mock_drive.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_update_file_metadata_rename_only(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = _live_meta(
        name="Old Name.txt", parents=["folder_a"]
    )
    mock_drive.files().update.return_value.execute.return_value = {
        "id": "f1", "name": "New Name.txt", "parents": ["folder_a"],
        "mimeType": "text/plain", "modifiedTime": "2026-08-01T00:00:00Z",
    }
    from gsuite_mcp.server import update_file_metadata
    result = await update_file_metadata("f1", name="New Name.txt")
    assert result["file_id"] == "f1"
    assert result["name"] == "New Name.txt"
    assert result["previous_name"] == "Old Name.txt"
    assert result["previous_parents"] == ["folder_a"]


@pytest.mark.asyncio
async def test_update_file_metadata_move_only(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = _live_meta(
        name="Doc", parents=["folder_a"]
    )
    mock_drive.files().update.return_value.execute.return_value = {
        "id": "f1", "name": "Doc", "parents": ["folder_b"],
        "mimeType": "text/plain", "modifiedTime": "2026-08-01T00:00:00Z",
    }
    from gsuite_mcp.server import update_file_metadata
    result = await update_file_metadata(
        "f1", add_parent_id="folder_b", remove_parent_id="folder_a"
    )
    assert result["parents"] == ["folder_b"]
    assert result["previous_parents"] == ["folder_a"]
    call_kwargs = mock_drive.files().update.call_args.kwargs
    assert call_kwargs["addParents"] == "folder_b"
    assert call_kwargs["removeParents"] == "folder_a"


@pytest.mark.asyncio
async def test_update_file_metadata_rename_and_move(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = _live_meta(
        name="Old Name", parents=["folder_a"]
    )
    mock_drive.files().update.return_value.execute.return_value = {
        "id": "f1", "name": "New Name", "parents": ["folder_b"],
        "mimeType": "text/plain", "modifiedTime": "2026-08-01T00:00:00Z",
    }
    from gsuite_mcp.server import update_file_metadata
    result = await update_file_metadata(
        "f1", name="New Name",
        add_parent_id="folder_b", remove_parent_id="folder_a",
    )
    assert result["name"] == "New Name"
    assert result["parents"] == ["folder_b"]
    assert result["previous_name"] == "Old Name"
    assert result["previous_parents"] == ["folder_a"]


@pytest.mark.asyncio
async def test_update_file_metadata_rename_google_doc_does_not_touch_content(
    mock_drive, mock_docs
):
    """Renaming a native Google Doc must go through files.update only —
    never through the Docs API, so document content is untouched."""
    mock_drive.files().get.return_value.execute.return_value = _live_meta(
        name="Old Doc", parents=["folder_a"]
    )
    mock_drive.files().update.return_value.execute.return_value = {
        "id": "f1", "name": "New Doc", "parents": ["folder_a"],
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-08-01T00:00:00Z",
    }
    from gsuite_mcp.server import update_file_metadata
    result = await update_file_metadata("f1", name="New Doc")
    assert result["name"] == "New Doc"
    mock_docs.assert_not_called()
    call_kwargs = mock_drive.files().update.call_args.kwargs
    assert call_kwargs["body"] == {"name": "New Doc"}
