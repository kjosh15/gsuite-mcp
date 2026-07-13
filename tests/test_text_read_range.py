"""Tests for text_ops.read_range and the text_read_range tool wrapper."""

from unittest.mock import patch, MagicMock

import pytest

from gsuite_mcp import text_ops, pagination


def _meta(**overrides):
    base = {"name": "notes.md", "mimeType": "text/markdown", "size": "100"}
    base.update(overrides)
    return base


def _mock_drive(content: bytes):
    svc = MagicMock()
    svc.files().get_media.return_value.execute.return_value = content
    return svc


@pytest.mark.asyncio
async def test_read_range_unsupported_mime():
    svc = _mock_drive(b"content")
    result = await text_ops.read_range(
        svc, "f1", _meta(mimeType="application/pdf"), None, None, 100_000, None,
    )
    assert result["error"] == "UNSUPPORTED_MIME"


@pytest.mark.asyncio
async def test_read_range_full_small_file():
    content = "\n".join(f"line{i}" for i in range(5)).encode()
    svc = _mock_drive(content)
    result = await text_ops.read_range(
        svc, "f1", _meta(size=str(len(content))), None, None, 100_000, None,
    )
    assert result["content"] == content.decode()
    assert result["total_lines"] == 5
    assert result["truncated"] is False
    assert result["next_cursor"] is None


@pytest.mark.asyncio
async def test_read_range_start_end_line_bounds():
    content = "\n".join(f"line{i}" for i in range(10)).encode()
    svc = _mock_drive(content)
    result = await text_ops.read_range(
        svc, "f1", _meta(size=str(len(content))), 2, 4, 100_000, None,
    )
    assert result["content"] == "line2\nline3\nline4"
    assert result["total_lines"] == 10


@pytest.mark.asyncio
async def test_read_range_fully_satisfied_end_line_not_truncated():
    """A fully-satisfied start_line/end_line window is not 'truncated' just because the file has more lines after it."""
    content = "\n".join(f"line{i}" for i in range(10)).encode()
    svc = _mock_drive(content)
    result = await text_ops.read_range(
        svc, "f1", _meta(size=str(len(content))), 2, 4, 100_000, None,
    )
    assert result["content"] == "line2\nline3\nline4"
    assert result["truncated"] is False
    assert result["next_cursor"] is None


@pytest.mark.asyncio
async def test_read_range_max_bytes_truncates_and_returns_cursor():
    content = "\n".join(f"line{i}" for i in range(1000)).encode()
    svc = _mock_drive(content)
    result = await text_ops.read_range(
        svc, "f1", _meta(size=str(len(content))), None, None, 50, None,
    )
    assert result["truncated"] is True
    assert result["next_cursor"] is not None
    assert len(result["content"].encode()) <= 50 or result["content"] == "line0"


@pytest.mark.asyncio
async def test_read_range_cursor_continues_from_offset():
    content = "\n".join(f"line{i}" for i in range(10)).encode()
    svc = _mock_drive(content)
    cursor = pagination.encode_cursor({"kind": "text_range", "offset": 5})
    result = await text_ops.read_range(
        svc, "f1", _meta(size=str(len(content))), None, None, 100_000, cursor,
    )
    assert result["content"] == "line5\nline6\nline7\nline8\nline9"
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_read_range_cursor_continuation_respects_original_end_line():
    """A budget-cut bounded read's next_cursor must not read past the original end_line."""
    content = "\n".join(f"line{i}" for i in range(10)).encode()
    svc = _mock_drive(content)

    all_lines: list[str] = []
    result = await text_ops.read_range(
        svc, "f1", _meta(size=str(len(content))), 2, 8, 15, None,
    )
    all_lines.extend(result["content"].split("\n"))
    cursor = result["next_cursor"]
    assert cursor is not None  # budget of 15 bytes can't fit lines 2-8 in one page

    while cursor is not None:
        page = await text_ops.read_range(
            svc, "f1", _meta(size=str(len(content))), None, None, 15, cursor,
        )
        all_lines.extend(page["content"].split("\n"))
        cursor = page["next_cursor"]

    # Must stop at line8 (end_line=8, inclusive) -- never reach line9.
    assert all_lines == [f"line{i}" for i in range(2, 9)]


@pytest.mark.asyncio
async def test_read_range_invalid_cursor():
    svc = _mock_drive(b"line0\nline1")
    result = await text_ops.read_range(
        svc, "f1", _meta(size="11"), None, None, 100_000, "not-a-valid-cursor!!",
    )
    assert result["error"] == "INVALID_CURSOR"


@pytest.mark.asyncio
async def test_text_read_range_tool_refuses_trashed():
    with patch("gsuite_mcp.auth.get_drive_service") as mock:
        drive = MagicMock()
        mock.return_value = drive
        drive.files().get.return_value.execute.return_value = {
            "name": "notes.md", "mimeType": "text/markdown", "size": "10",
            "trashed": True, "trashedTime": "2026-07-01T00:00:00Z",
        }
        from gsuite_mcp.server import text_read_range
        result = await text_read_range("f1")
        assert result["error"] == "TRASHED_FILE"
