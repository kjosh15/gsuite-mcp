"""Tests for create_backup_copy in drive_ops."""

import pytest
from unittest.mock import MagicMock
from gsuite_mcp.drive_ops import create_backup_copy


@pytest.mark.asyncio
async def test_create_backup_copy_default_folder():
    """Creates a copy in the same folder with timestamped name."""
    service = MagicMock()
    service.files().get.return_value.execute.return_value = {
        "name": "My Document",
        "parents": ["folder123"],
    }
    service.files().copy.return_value.execute.return_value = {
        "id": "backup_file_id",
        "name": "My Document__autobackup_2026-06-04T12:00:00Z",
    }

    result = await create_backup_copy(service, "original_id")

    assert result["backup_file_id"] == "backup_file_id"
    assert "autobackup" in result["backup_file_name"]

    copy_call = service.files().copy.call_args
    body = copy_call.kwargs.get("body") or copy_call[1].get("body")
    assert body["parents"] == ["folder123"]
    assert "__autobackup_" in body["name"]


@pytest.mark.asyncio
async def test_create_backup_copy_custom_folder():
    """Uses backup_folder_id when provided."""
    service = MagicMock()
    service.files().get.return_value.execute.return_value = {
        "name": "My Document",
        "parents": ["folder123"],
    }
    service.files().copy.return_value.execute.return_value = {
        "id": "backup_id",
        "name": "My Document__autobackup_2026-06-04T12:00:00Z",
    }

    await create_backup_copy(
        service, "original_id", backup_folder_id="custom_folder",
    )

    copy_call = service.files().copy.call_args
    body = copy_call.kwargs.get("body") or copy_call[1].get("body")
    assert body["parents"] == ["custom_folder"]
