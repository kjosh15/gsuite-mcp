"""Tests for server.py auto-snapshot wiring on replace_section and gdoc_batch_replace."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_doc(*paragraphs):
    """Build a minimal Google Docs body structure.

    Each paragraph is a tuple: (start, end, text, named_style).
    """
    content = []
    for start, end, text, named_style in paragraphs:
        content.append({
            "startIndex": start,
            "endIndex": end,
            "paragraph": {
                "paragraphStyle": {"namedStyleType": named_style},
                "elements": [
                    {
                        "startIndex": start,
                        "endIndex": end,
                        "textRun": {"content": text},
                    }
                ],
            },
        })
    return {"body": {"content": content}}


@pytest.fixture
def mock_services():
    with patch("gsuite_mcp.auth.get_drive_service") as mock_drive, \
         patch("gsuite_mcp.auth.get_docs_service") as mock_docs:
        drive = MagicMock()
        docs = MagicMock()
        mock_drive.return_value = drive
        mock_docs.return_value = docs
        yield {"drive": drive, "docs": docs}


# -------------------------------------------------------------------
# replace_section: new params forwarded
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_section_passes_dry_run(mock_services):
    """dry_run param is forwarded from server to docs_ops.replace_section."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().get.return_value.execute.return_value = {
        "name": "My Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-06-04T12:00:00Z",
    }

    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 30, "Old body text here.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
    )
    docs.documents().get.return_value.execute.return_value = doc

    from gsuite_mcp.server import replace_section
    result = await replace_section(
        file_id="d1",
        section_heading="Chapter 1",
        new_content="New.\n",
        dry_run=True,
    )

    assert result.get("dry_run") is True
    assert "error" not in result
    docs.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_replace_section_passes_expected_delete_chars(mock_services):
    """expected_delete_chars is forwarded; mismatch triggers DELETE_CHARS_MISMATCH."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().get.return_value.execute.return_value = {
        "name": "My Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-06-04T12:00:00Z",
    }

    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 30, "Old body text here.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
    )
    docs.documents().get.return_value.execute.return_value = doc

    from gsuite_mcp.server import replace_section
    result = await replace_section(
        file_id="d1",
        section_heading="Chapter 1",
        new_content="New.\n",
        expected_delete_chars=999,
    )

    assert result["error"] == "DELETE_CHARS_MISMATCH"
    assert result["expected"] == 999
    assert result["actual"] == 20
    docs.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_replace_section_passes_confirm_delete_chars(mock_services):
    """confirm_delete_chars is forwarded; bypasses blast-radius guard."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().get.return_value.execute.return_value = {
        "name": "My Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-06-04T12:00:00Z",
        "parents": ["folder1"],
    }

    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 6663, "x" * 6652 + "\n", "NORMAL_TEXT"),
        (6663, 6673, "Chapter 2\n", "HEADING_1"),
    )
    docs.documents().get.return_value.execute.return_value = doc
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    # Mock create_backup_copy
    drive.files().copy.return_value.execute.return_value = {
        "id": "backup_abc", "name": "My Doc__autobackup_2026-06-04T12:00:00Z",
    }

    from gsuite_mcp.server import replace_section
    result = await replace_section(
        file_id="d1",
        section_heading="Chapter 1",
        new_content="y" * 200 + "\n",
        confirm_delete_chars=6653,
    )

    assert "error" not in result
    assert result["characters_deleted"] == 6653


# -------------------------------------------------------------------
# replace_section: auto-snapshot
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_section_auto_snapshot_on_confirm(mock_services):
    """When confirm_delete_chars is provided, create_backup_copy is called."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().get.return_value.execute.return_value = {
        "name": "My Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-06-04T12:00:00Z",
        "parents": ["folder1"],
    }

    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 6663, "x" * 6652 + "\n", "NORMAL_TEXT"),
        (6663, 6673, "Chapter 2\n", "HEADING_1"),
    )
    docs.documents().get.return_value.execute.return_value = doc
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    drive.files().copy.return_value.execute.return_value = {
        "id": "backup_abc", "name": "My Doc__autobackup_2026-06-04T12:00:00Z",
    }

    from gsuite_mcp.server import replace_section
    result = await replace_section(
        file_id="d1",
        section_heading="Chapter 1",
        new_content="y" * 200 + "\n",
        confirm_delete_chars=6653,
    )

    assert "error" not in result
    assert result["backup_file_id"] == "backup_abc"
    assert result["backup_file_name"] == "My Doc__autobackup_2026-06-04T12:00:00Z"
    drive.files().copy.assert_called_once()


@pytest.mark.asyncio
async def test_replace_section_no_snapshot_without_confirm(mock_services):
    """When confirm_delete_chars is not passed, no backup is created."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().get.return_value.execute.return_value = {
        "name": "My Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-06-04T12:00:00Z",
    }

    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 30, "Old body text here.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
    )
    docs.documents().get.return_value.execute.return_value = doc
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    from gsuite_mcp.server import replace_section
    result = await replace_section(
        file_id="d1",
        section_heading="Chapter 1",
        new_content="New body.\n",
    )

    assert "error" not in result
    assert "backup_file_id" not in result
    # files().copy should NOT be called (no backup)
    drive.files().copy.assert_not_called()


@pytest.mark.asyncio
async def test_replace_section_no_snapshot_on_error(mock_services):
    """When the ops call returns an error, no backup info is merged."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().get.return_value.execute.return_value = {
        "name": "My Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-06-04T12:00:00Z",
        "parents": ["folder1"],
    }

    # Heading not found -> error
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
    )
    docs.documents().get.return_value.execute.return_value = doc

    drive.files().copy.return_value.execute.return_value = {
        "id": "backup_abc", "name": "My Doc__autobackup_2026-06-04",
    }

    from gsuite_mcp.server import replace_section
    result = await replace_section(
        file_id="d1",
        section_heading="Nonexistent",
        new_content="New.\n",
        confirm_delete_chars=100,
    )

    # Error result should NOT have backup info merged
    assert result["error"] == "HEADING_NOT_FOUND"
    assert "backup_file_id" not in result


# -------------------------------------------------------------------
# gdoc_batch_replace: confirm_delete_chars forwarded
# -------------------------------------------------------------------


def _make_batch_doc(*paragraphs_text: str) -> dict:
    """Build minimal doc structure for batch replace tests (1-based indexing)."""
    content = []
    idx = 1
    for text in paragraphs_text:
        full_text = text + "\n"
        content.append({
            "startIndex": idx,
            "endIndex": idx + len(full_text),
            "paragraph": {
                "elements": [{
                    "startIndex": idx,
                    "endIndex": idx + len(full_text),
                    "textRun": {"content": full_text},
                }],
            },
        })
        idx += len(full_text)
    return {"body": {"content": content}}


@pytest.mark.asyncio
async def test_gdoc_batch_replace_passes_confirm_delete_chars(mock_services):
    """confirm_delete_chars is forwarded to gdoc_ops.batch_replace."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().get.return_value.execute.return_value = {
        "name": "Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-06-04T12:00:00Z",
        "parents": ["folder1"],
    }

    long_text = "x" * 499
    doc = _make_batch_doc(long_text)
    docs.documents().get.return_value.execute.return_value = doc
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    drive.revisions().list.return_value.execute.return_value = {
        "revisions": [{"id": "r1"}]
    }
    drive.files().copy.return_value.execute.return_value = {
        "id": "backup_xyz", "name": "Doc__autobackup_2026-06-04T12:00:00Z",
    }

    with patch.dict("os.environ", {"GDOC_REVIEW_DOC_IDS": "protected1"}):
        from gsuite_mcp.server import gdoc_batch_replace
        result = await gdoc_batch_replace(
            file_id="f1",
            edits=[{"find_text": long_text, "replace_text": "y", "expected_count": 1}],
            confirm_delete_chars=499,
        )

    assert result["committed"] is True
    assert result["backup_file_id"] == "backup_xyz"


# -------------------------------------------------------------------
# gdoc_batch_replace: auto-snapshot
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gdoc_batch_replace_auto_snapshot_on_confirm(mock_services):
    """Backup is created when confirm_delete_chars is provided and mutation succeeds."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().get.return_value.execute.return_value = {
        "name": "Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-06-04T12:00:00Z",
        "parents": ["folder1"],
    }

    long_text = "x" * 499
    doc = _make_batch_doc(long_text)
    docs.documents().get.return_value.execute.return_value = doc
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    drive.revisions().list.return_value.execute.return_value = {
        "revisions": [{"id": "r1"}]
    }
    drive.files().copy.return_value.execute.return_value = {
        "id": "backup_xyz", "name": "Doc__autobackup_2026-06-04T12:00:00Z",
    }

    with patch.dict("os.environ", {"GDOC_REVIEW_DOC_IDS": "protected1"}):
        from gsuite_mcp.server import gdoc_batch_replace
        result = await gdoc_batch_replace(
            file_id="f1",
            edits=[{"find_text": long_text, "replace_text": "y", "expected_count": 1}],
            confirm_delete_chars=499,
        )

    assert "error" not in result
    assert result["backup_file_id"] == "backup_xyz"
    assert result["backup_file_name"] == "Doc__autobackup_2026-06-04T12:00:00Z"
    drive.files().copy.assert_called_once()


@pytest.mark.asyncio
async def test_gdoc_batch_replace_no_snapshot_without_confirm(mock_services):
    """No backup when confirm_delete_chars is not passed."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().get.return_value.execute.return_value = {
        "name": "Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-06-04T12:00:00Z",
    }

    doc = _make_batch_doc("Hello world")
    docs.documents().get.return_value.execute.return_value = doc
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    drive.revisions().list.return_value.execute.return_value = {
        "revisions": [{"id": "r1"}]
    }

    with patch.dict("os.environ", {"GDOC_REVIEW_DOC_IDS": "protected1"}):
        from gsuite_mcp.server import gdoc_batch_replace
        result = await gdoc_batch_replace(
            file_id="f1",
            edits=[{"find_text": "Hello", "replace_text": "Hi"}],
        )

    assert "error" not in result
    assert "backup_file_id" not in result
    drive.files().copy.assert_not_called()


@pytest.mark.asyncio
async def test_gdoc_batch_replace_no_snapshot_on_error(mock_services):
    """When the ops call returns an error, no backup info is merged."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().get.return_value.execute.return_value = {
        "name": "Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-06-04T12:00:00Z",
        "parents": ["folder1"],
    }

    # blast-radius trips with wrong confirm value
    long_text = "x" * 499
    doc = _make_batch_doc(long_text)
    docs.documents().get.return_value.execute.return_value = doc

    drive.revisions().list.return_value.execute.return_value = {
        "revisions": [{"id": "r1"}]
    }
    drive.files().copy.return_value.execute.return_value = {
        "id": "backup_xyz", "name": "backup",
    }

    with patch.dict("os.environ", {"GDOC_REVIEW_DOC_IDS": "protected1"}):
        from gsuite_mcp.server import gdoc_batch_replace
        # Pass wrong confirm value to trigger BLAST_RADIUS_EXCEEDED
        result = await gdoc_batch_replace(
            file_id="f1",
            edits=[{"find_text": long_text, "replace_text": "y", "expected_count": 1}],
            confirm_delete_chars=1,  # wrong value, guard still trips
        )

    assert result["error"] == "BLAST_RADIUS_EXCEEDED"
    # Backup was created (since confirm_delete_chars was provided), but
    # backup info should NOT be merged into error results
    assert "backup_file_id" not in result
