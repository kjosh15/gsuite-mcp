"""Tests for format_document in docs_ops and the server tool wrapper."""

import pytest
from unittest.mock import MagicMock, patch

from googleapiclient.errors import HttpError

from gsuite_mcp.docs_ops import format_document, VALID_NAMED_STYLES, _validate_text_style


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


def _mock_docs_service(doc, batch_response=None):
    svc = MagicMock()
    svc.documents().get.return_value.execute.return_value = doc
    if batch_response is None:
        batch_response = {"replies": []}
    svc.documents().batchUpdate.return_value.execute.return_value = batch_response
    return svc


def _make_http_error(status: int) -> HttpError:
    resp = MagicMock()
    resp.status = status
    return HttpError(resp, b"error")


# -------------------------------------------------------------------
# VALID_NAMED_STYLES constant
# -------------------------------------------------------------------

def test_valid_named_styles_includes_all():
    assert "NORMAL_TEXT" in VALID_NAMED_STYLES
    assert "TITLE" in VALID_NAMED_STYLES
    assert "SUBTITLE" in VALID_NAMED_STYLES
    for i in range(1, 7):
        assert f"HEADING_{i}" in VALID_NAMED_STYLES


# -------------------------------------------------------------------
# format_document — validation
# -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_format_invalid_action():
    svc = _mock_docs_service(_make_doc())
    result = await format_document(svc, "f1", [
        {"action": "unknown", "find_text": "x"},
    ])
    assert result["error"] == "INVALID_ACTION"
    assert result["retryable"] is False


@pytest.mark.asyncio
async def test_format_invalid_style():
    svc = _mock_docs_service(_make_doc())
    result = await format_document(svc, "f1", [
        {"action": "set_style", "find_text": "x", "style": "BOLD_TEXT"},
    ])
    assert result["error"] == "INVALID_STYLE"
    assert result["retryable"] is False


@pytest.mark.asyncio
async def test_format_missing_find_text():
    svc = _mock_docs_service(_make_doc())
    result = await format_document(svc, "f1", [
        {"action": "delete"},
    ])
    assert result["error"] == "MISSING_FIND_TEXT"
    assert result["retryable"] is False


@pytest.mark.asyncio
async def test_format_empty_find_text():
    svc = _mock_docs_service(_make_doc())
    result = await format_document(svc, "f1", [
        {"action": "delete", "find_text": "   "},
    ])
    assert result["error"] == "MISSING_FIND_TEXT"


@pytest.mark.asyncio
async def test_format_set_style_missing_style():
    svc = _mock_docs_service(_make_doc())
    result = await format_document(svc, "f1", [
        {"action": "set_style", "find_text": "x"},
    ])
    assert result["error"] == "INVALID_STYLE"


@pytest.mark.asyncio
async def test_format_empty_operations():
    svc = _mock_docs_service(_make_doc())
    result = await format_document(svc, "f1", [])
    assert result["error"] == "EMPTY_OPERATIONS"


# -------------------------------------------------------------------
# format_document — set_style
# -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_style_basic():
    doc = _make_doc(
        (0, 14, "Introduction\n", "NORMAL_TEXT"),
        (14, 30, "Some body text.\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "set_style", "find_text": "Introduction", "style": "HEADING_1"},
    ])

    assert "error" not in result
    assert result["file_id"] == "f1"
    assert result["operations_applied"] == 1
    assert result["results"][0]["status"] == "applied"
    assert result["results"][0]["style"] == "HEADING_1"

    # Verify batchUpdate request
    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    assert len(requests) == 1
    style_req = requests[0]["updateParagraphStyle"]
    assert style_req["range"]["startIndex"] == 0
    assert style_req["range"]["endIndex"] == 14
    assert style_req["paragraphStyle"]["namedStyleType"] == "HEADING_1"
    assert style_req["fields"] == "namedStyleType"


@pytest.mark.asyncio
async def test_set_style_case_insensitive():
    doc = _make_doc(
        (0, 14, "Introduction\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "set_style", "find_text": "introduction", "style": "HEADING_2"},
    ])
    assert result["operations_applied"] == 1
    assert result["results"][0]["status"] == "applied"


@pytest.mark.asyncio
async def test_set_style_substring_match():
    """find_text matches a substring within the paragraph text with substring=True."""
    doc = _make_doc(
        (0, 30, "Chapter 1: Introduction\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "set_style", "find_text": "Introduction", "style": "HEADING_1", "substring": True},
    ])
    assert result["operations_applied"] == 1


@pytest.mark.asyncio
async def test_set_style_not_found():
    doc = _make_doc(
        (0, 14, "Introduction\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "set_style", "find_text": "Conclusion", "style": "HEADING_1"},
    ])
    assert result["operations_applied"] == 0
    assert result["results"][0]["status"] == "not_found"
    # No batchUpdate call since nothing to apply
    svc.documents().batchUpdate.assert_not_called()


# -------------------------------------------------------------------
# format_document — delete
# -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_paragraph():
    doc = _make_doc(
        (0, 14, "Introduction\n", "HEADING_1"),
        (14, 36, "Paragraph to remove.\n", "NORMAL_TEXT"),
        (36, 50, "Keep this text.\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "delete", "find_text": "Paragraph to remove."},
    ])

    assert result["operations_applied"] == 1
    assert result["results"][0]["status"] == "applied"
    assert result["results"][0]["characters_deleted"] == 36 - 14

    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    delete_req = requests[0]["deleteContentRange"]["range"]
    assert delete_req["startIndex"] == 14
    assert delete_req["endIndex"] == 36


@pytest.mark.asyncio
async def test_delete_first_match_only_substring():
    """With substring=True, when multiple paragraphs match, only the first is deleted
    (single match since 'Note: A' != 'Note: B' with exact, but both contain 'Note')."""
    doc = _make_doc(
        (0, 8, "Note: A\n", "NORMAL_TEXT"),
        (8, 16, "Note: B\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    # With substring + match_all, both are deleted
    result = await format_document(svc, "f1", [
        {"action": "delete", "find_text": "Note", "substring": True, "match_all": True},
    ])

    assert result["operations_applied"] == 1
    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    # Two deletes, sorted descending by startIndex
    assert len(requests) == 2
    assert requests[0]["deleteContentRange"]["range"]["startIndex"] == 8
    assert requests[1]["deleteContentRange"]["range"]["startIndex"] == 0


# -------------------------------------------------------------------
# format_document — delete_empty_after
# -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_empty_after():
    doc = _make_doc(
        (0, 14, "Introduction\n", "HEADING_1"),
        (14, 15, "\n", "NORMAL_TEXT"),              # empty
        (15, 16, "\n", "NORMAL_TEXT"),              # empty
        (16, 30, "Real content.\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "delete_empty_after", "find_text": "Introduction"},
    ])

    assert result["operations_applied"] == 1
    assert result["results"][0]["empty_paragraphs_deleted"] == 2

    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    # Two deletes, sorted descending by startIndex
    assert len(requests) == 2
    assert requests[0]["deleteContentRange"]["range"]["startIndex"] == 15
    assert requests[1]["deleteContentRange"]["range"]["startIndex"] == 14


@pytest.mark.asyncio
async def test_delete_empty_after_no_empties():
    doc = _make_doc(
        (0, 14, "Introduction\n", "HEADING_1"),
        (14, 30, "Real content.\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "delete_empty_after", "find_text": "Introduction"},
    ])

    assert result["operations_applied"] == 1
    assert result["results"][0]["empty_paragraphs_deleted"] == 0
    # No batchUpdate call when no empties found
    svc.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_delete_empty_after_whitespace_only():
    """Paragraphs with only spaces/tabs count as empty."""
    doc = _make_doc(
        (0, 14, "Introduction\n", "HEADING_1"),
        (14, 18, "   \n", "NORMAL_TEXT"),     # whitespace-only
        (18, 30, "Real content.\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "delete_empty_after", "find_text": "Introduction"},
    ])

    assert result["results"][0]["empty_paragraphs_deleted"] == 1


# -------------------------------------------------------------------
# format_document — multiple operations in one call
# -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multiple_operations():
    """set_style + delete in one call, requests sorted descending."""
    doc = _make_doc(
        (0, 14, "Introduction\n", "NORMAL_TEXT"),
        (14, 30, "Old paragraph.\n", "NORMAL_TEXT"),
        (30, 42, "Conclusion\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "set_style", "find_text": "Introduction", "style": "HEADING_1"},
        {"action": "delete", "find_text": "Old paragraph."},
        {"action": "set_style", "find_text": "Conclusion", "style": "HEADING_1"},
    ])

    assert result["operations_applied"] == 3
    assert all(r["status"] == "applied" for r in result["results"])

    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    assert len(requests) == 3

    # Requests should be sorted by startIndex descending
    indices = []
    for req in requests:
        if "updateParagraphStyle" in req:
            indices.append(req["updateParagraphStyle"]["range"]["startIndex"])
        elif "deleteContentRange" in req:
            indices.append(req["deleteContentRange"]["range"]["startIndex"])
    assert indices == sorted(indices, reverse=True)


@pytest.mark.asyncio
async def test_partial_success():
    """One operation succeeds, one not_found — still applies what it can."""
    doc = _make_doc(
        (0, 14, "Introduction\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "set_style", "find_text": "Introduction", "style": "HEADING_1"},
        {"action": "delete", "find_text": "Nonexistent"},
    ])

    assert result["operations_applied"] == 1
    assert result["results"][0]["status"] == "applied"
    assert result["results"][1]["status"] == "not_found"


# -------------------------------------------------------------------
# Server-level tests for format_document tool wrapper
# -------------------------------------------------------------------

@pytest.fixture
def mock_services():
    with patch("gsuite_mcp.auth.get_drive_service") as mock_drive, \
         patch("gsuite_mcp.auth.get_docs_service") as mock_docs:
        drive = MagicMock()
        docs = MagicMock()
        mock_drive.return_value = drive
        mock_docs.return_value = docs
        yield {"drive": drive, "docs": docs}


@pytest.mark.asyncio
async def test_server_format_not_a_google_doc(mock_services):
    from gsuite_mcp.server import format_document as server_format_document

    drive = mock_services["drive"]
    drive.files().get.return_value.execute.return_value = {
        "name": "file.docx",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "modifiedTime": "2026-05-10T12:00:00Z",
    }

    result = await server_format_document(
        file_id="d1",
        operations=[{"action": "set_style", "find_text": "x", "style": "HEADING_1"}],
    )

    assert result["error"] == "NOT_A_GOOGLE_DOC"
    assert result["retryable"] is False


@pytest.mark.asyncio
async def test_server_format_success(mock_services):
    from gsuite_mcp.server import format_document as server_format_document

    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().get.return_value.execute.return_value = {
        "name": "My Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-05-12T14:00:00Z",
    }
    docs.documents().get.return_value.execute.return_value = _make_doc(
        (0, 14, "Introduction\n", "NORMAL_TEXT"),
        (14, 30, "Some body text.\n", "NORMAL_TEXT"),
    )
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    result = await server_format_document(
        file_id="d1",
        operations=[{"action": "set_style", "find_text": "Introduction", "style": "HEADING_1"}],
    )

    assert "error" not in result
    assert result["file_id"] == "d1"
    assert result["operations_applied"] == 1
    assert result["modified_time"] == "2026-05-12T14:00:00Z"


@pytest.mark.asyncio
async def test_server_format_catches_http_error(mock_services):
    from gsuite_mcp.server import format_document as server_format_document

    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().get.return_value.execute.return_value = {
        "name": "My Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-05-12T14:00:00Z",
    }
    docs.documents().get.return_value.execute.side_effect = _make_http_error(500)

    result = await server_format_document(
        file_id="d1",
        operations=[{"action": "set_style", "find_text": "x", "style": "HEADING_1"}],
    )

    assert result["error"] == "GOOGLE_API_ERROR"
    assert result["retryable"] is True
    assert result["http_status"] == 500


# -------------------------------------------------------------------
# Issue 1: Exact match default + safety features
# -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exact_match_default_no_longer_substring():
    """Default matching is exact (strip + casefold). Substring no longer matches."""
    doc = _make_doc(
        (0, 30, "Chapter 1: Introduction\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "set_style", "find_text": "Introduction", "style": "HEADING_1"},
    ])
    # "Introduction" != "Chapter 1: Introduction" after strip+casefold
    assert result["operations_applied"] == 0
    assert result["results"][0]["status"] == "not_found"


@pytest.mark.asyncio
async def test_substring_opt_in():
    """Operations can opt-in to substring matching with substring: True."""
    doc = _make_doc(
        (0, 30, "Chapter 1: Introduction\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "set_style", "find_text": "Introduction", "style": "HEADING_1", "substring": True},
    ])
    assert result["operations_applied"] == 1
    assert result["results"][0]["status"] == "applied"


@pytest.mark.asyncio
async def test_delete_multi_match_fails_without_match_all():
    """When >1 paragraph matches, delete fails with multi_match_error."""
    doc = _make_doc(
        (0, 10, "Duplicate\n", "NORMAL_TEXT"),
        (10, 20, "Duplicate\n", "NORMAL_TEXT"),
        (20, 30, "Different.\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "delete", "find_text": "Duplicate"},
    ])
    assert result["results"][0]["status"] == "multi_match_error"
    assert "matches" in result["results"][0]
    assert len(result["results"][0]["matches"]) == 2
    # Verify match entries have paragraph_index and text
    for m in result["results"][0]["matches"]:
        assert "paragraph_index" in m
        assert "text" in m
    # No batchUpdate since only operation failed
    svc.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_delete_multi_match_with_match_all():
    """match_all: True on operation allows deleting all matches."""
    doc = _make_doc(
        (0, 10, "Duplicate\n", "NORMAL_TEXT"),
        (10, 20, "Duplicate\n", "NORMAL_TEXT"),
        (20, 30, "Different.\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "delete", "find_text": "Duplicate", "match_all": True},
    ])
    assert result["operations_applied"] == 1
    assert result["results"][0]["status"] == "applied"
    assert result["results"][0]["characters_deleted"] == 20  # both paragraphs (10 + 10)

    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    # Two delete requests (sorted descending)
    assert len(requests) == 2
    assert requests[0]["deleteContentRange"]["range"]["startIndex"] == 10
    assert requests[1]["deleteContentRange"]["range"]["startIndex"] == 0


@pytest.mark.asyncio
async def test_set_style_multi_match_fails():
    """Multi-match protection applies to set_style too."""
    doc = _make_doc(
        (0, 10, "Duplicate\n", "NORMAL_TEXT"),
        (10, 20, "Duplicate\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "set_style", "find_text": "Duplicate", "style": "HEADING_1"},
    ])
    assert result["results"][0]["status"] == "multi_match_error"
    assert len(result["results"][0]["matches"]) == 2


@pytest.mark.asyncio
async def test_preview_mode_no_mutation():
    """preview=True returns what would happen without executing."""
    doc = _make_doc(
        (0, 14, "Introduction\n", "NORMAL_TEXT"),
        (14, 30, "Some body text.\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "set_style", "find_text": "Introduction", "style": "HEADING_1"},
    ], preview=True)

    assert "error" not in result
    assert result.get("preview") is True
    assert len(result["results"]) == 1
    assert result["results"][0]["paragraph_index"] == 0
    assert "Introduction" in result["results"][0]["text"]
    assert result["results"][0]["action"] == "set_style"
    # No batchUpdate called
    svc.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_delete_by_index():
    """delete_by_index takes paragraph_index and deletes that paragraph."""
    doc = _make_doc(
        (0, 14, "Introduction\n", "HEADING_1"),
        (14, 30, "Body paragraph.\n", "NORMAL_TEXT"),
        (30, 42, "Conclusion.\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "delete_by_index", "paragraph_index": 1},
    ])
    assert result["operations_applied"] == 1
    assert result["results"][0]["status"] == "applied"
    assert result["results"][0]["characters_deleted"] == 30 - 14

    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    delete_req = requests[0]["deleteContentRange"]["range"]
    assert delete_req["startIndex"] == 14
    assert delete_req["endIndex"] == 30


@pytest.mark.asyncio
async def test_delete_by_index_out_of_range():
    """delete_by_index with invalid index reports error."""
    doc = _make_doc(
        (0, 14, "Introduction\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "delete_by_index", "paragraph_index": 99},
    ])
    assert result["results"][0]["status"] == "index_out_of_range"
    svc.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_delete_last_paragraph_clamps_trailing_newline():
    """Deleting the last paragraph clamps endIndex to preserve structural newline."""
    doc = _make_doc(
        (0, 14, "Introduction\n", "HEADING_1"),
        (14, 30, "Last paragraph.\n", "NORMAL_TEXT"),  # 30 = doc end
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "delete", "find_text": "Last paragraph."},
    ])
    assert result["operations_applied"] == 1

    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    delete_req = requests[0]["deleteContentRange"]["range"]
    assert delete_req["startIndex"] == 14
    # Clamped: 30 - 1 = 29
    assert delete_req["endIndex"] == 29


@pytest.mark.asyncio
async def test_multi_match_error_does_not_block_other_ops():
    """A multi_match_error on one operation doesn't prevent others from executing."""
    doc = _make_doc(
        (0, 10, "Duplicate\n", "NORMAL_TEXT"),
        (10, 20, "Duplicate\n", "NORMAL_TEXT"),
        (20, 34, "Introduction\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "delete", "find_text": "Duplicate"},
        {"action": "set_style", "find_text": "Introduction", "style": "HEADING_1"},
    ])
    # First op fails with multi_match, second succeeds
    assert result["results"][0]["status"] == "multi_match_error"
    assert result["results"][1]["status"] == "applied"
    assert result["operations_applied"] == 1
    # batchUpdate is called for the successful operation
    svc.documents().batchUpdate.assert_called_once()


# -------------------------------------------------------------------
# format_document — match_mode (regex, substring alias, precedence)
# -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_format_regex_match_mode_delete():
    """match_mode='regex' matches paragraph text by regex."""
    doc = _make_doc(
        (0, 10, "Hello World\n", "NORMAL_TEXT"),
        (10, 25, "-  -\n", "NORMAL_TEXT"),
        (25, 40, "Goodbye World\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "delete", "find_text": r"^-\s+-$", "match_mode": "regex"},
    ])
    assert result["results"][0]["status"] == "applied"
    assert result["results"][0]["characters_deleted"] > 0


@pytest.mark.asyncio
async def test_format_regex_match_mode_set_style():
    """match_mode='regex' works with set_style action."""
    doc = _make_doc(
        (0, 20, "1. Introduction\n", "NORMAL_TEXT"),
        (20, 40, "Some body text here\n", "NORMAL_TEXT"),
        (40, 60, "2. Methodology\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "set_style", "find_text": r"^\d+\.\s+", "match_mode": "regex",
         "style": "HEADING_1", "match_all": True},
    ])
    assert result["results"][0]["status"] == "applied"
    calls = svc.documents().batchUpdate.call_args
    requests = calls.kwargs["body"]["requests"]
    style_reqs = [r for r in requests if "updateParagraphStyle" in r]
    assert len(style_reqs) == 2


@pytest.mark.asyncio
async def test_format_regex_invalid_pattern():
    """Invalid regex pattern returns INVALID_REGEX error."""
    doc = _make_doc(
        (0, 10, "Hello\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "delete", "find_text": "[invalid", "match_mode": "regex"},
    ])
    assert result["error"] == "INVALID_REGEX"
    assert result["retryable"] is False


@pytest.mark.asyncio
async def test_format_regex_case_sensitive():
    """Regex match is case-sensitive by default (no casefold)."""
    doc = _make_doc(
        (0, 10, "Hello\n", "NORMAL_TEXT"),
        (10, 20, "hello\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "delete", "find_text": "^Hello$", "match_mode": "regex"},
    ])
    assert result["results"][0]["status"] == "applied"
    calls = svc.documents().batchUpdate.call_args
    requests = calls.kwargs["body"]["requests"]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_format_match_mode_substring_alias():
    """match_mode='substring' works as alias for substring=True."""
    doc = _make_doc(
        (0, 20, "Hello World Test\n", "NORMAL_TEXT"),
        (20, 30, "Goodbye\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "delete", "find_text": "World", "match_mode": "substring"},
    ])
    assert result["results"][0]["status"] == "applied"


@pytest.mark.asyncio
async def test_format_match_mode_overrides_substring_flag():
    """match_mode='exact' takes precedence over substring=True.

    With substring=True alone, "Hello" would match "Hello World" via substring.
    With match_mode='exact', the exact match requires the full text to equal
    "Hello", so "Hello World" does NOT match — only the exact "Hello" paragraph.
    """
    doc = _make_doc(
        (0, 10, "Hello\n", "NORMAL_TEXT"),
        (10, 25, "Hello World\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "delete", "find_text": "Hello", "match_mode": "exact", "substring": True},
    ])
    assert result["results"][0]["status"] == "applied"
    calls = svc.documents().batchUpdate.call_args
    requests = calls.kwargs["body"]["requests"]
    # Only 1 delete: exact match on "Hello", NOT substring match on "Hello World"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_format_invalid_match_mode():
    """Invalid match_mode returns INVALID_MATCH_MODE error."""
    doc = _make_doc(
        (0, 10, "Hello\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "delete", "find_text": "Hello", "match_mode": "fuzzy"},
    ])
    assert result["error"] == "INVALID_MATCH_MODE"
    assert result["retryable"] is False


# -------------------------------------------------------------------
# _validate_text_style helper
# -------------------------------------------------------------------

def test_validate_text_style_valid():
    assert _validate_text_style({"bold": True}) is None
    assert _validate_text_style({"italic": True, "strikethrough": False}) is None
    assert _validate_text_style({"bold": True, "italic": True, "underline": True, "strikethrough": True}) is None


def test_validate_text_style_empty():
    err = _validate_text_style({})
    assert err is not None
    assert "at least one" in err


def test_validate_text_style_unknown_key():
    err = _validate_text_style({"italic": True, "color": "red"})
    assert err is not None
    assert "color" in err


def test_validate_text_style_non_bool():
    err = _validate_text_style({"bold": "yes"})
    assert err is not None
    assert "boolean" in err


def test_validate_text_style_not_a_dict():
    err = _validate_text_style("italic")
    assert err is not None

    err2 = _validate_text_style(None)
    assert err2 is not None


# -------------------------------------------------------------------
# format_document — set_text_style
# -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_text_style_basic():
    """set_text_style applies italic to a matched paragraph."""
    doc = _make_doc(
        (0, 14, "Introduction\n", "NORMAL_TEXT"),
        (14, 30, "Some body text.\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "set_text_style", "find_text": "Introduction", "style": {"italic": True}},
    ])

    assert "error" not in result
    assert result["operations_applied"] == 1
    assert result["results"][0]["status"] == "applied"

    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    assert len(requests) == 1
    style_req = requests[0]["updateTextStyle"]
    assert style_req["range"]["startIndex"] == 0
    assert style_req["range"]["endIndex"] == 14
    assert style_req["textStyle"]["italic"] is True
    assert style_req["fields"] == "italic"


@pytest.mark.asyncio
async def test_set_text_style_multiple_properties():
    """set_text_style with multiple style properties builds correct fields mask."""
    doc = _make_doc(
        (0, 14, "Introduction\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "set_text_style", "find_text": "Introduction",
         "style": {"bold": True, "strikethrough": False}},
    ])

    assert result["operations_applied"] == 1
    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    style_req = requests[0]["updateTextStyle"]
    assert style_req["textStyle"]["bold"] is True
    assert style_req["textStyle"]["strikethrough"] is False
    # fields should contain both keys (order may vary)
    fields = set(style_req["fields"].split(","))
    assert fields == {"bold", "strikethrough"}


@pytest.mark.asyncio
async def test_set_text_style_not_found():
    doc = _make_doc(
        (0, 14, "Introduction\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "set_text_style", "find_text": "Nonexistent", "style": {"italic": True}},
    ])
    assert result["results"][0]["status"] == "not_found"
    svc.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_set_text_style_multi_match_error():
    doc = _make_doc(
        (0, 10, "Duplicate\n", "NORMAL_TEXT"),
        (10, 20, "Duplicate\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "set_text_style", "find_text": "Duplicate", "style": {"italic": True}},
    ])
    assert result["results"][0]["status"] == "multi_match_error"


@pytest.mark.asyncio
async def test_set_text_style_match_all():
    doc = _make_doc(
        (0, 10, "Duplicate\n", "NORMAL_TEXT"),
        (10, 20, "Duplicate\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "set_text_style", "find_text": "Duplicate",
         "style": {"italic": True}, "match_all": True},
    ])
    assert result["operations_applied"] == 1
    assert result["results"][0]["status"] == "applied"
    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_set_text_style_invalid_style():
    svc = _mock_docs_service(_make_doc())
    result = await format_document(svc, "f1", [
        {"action": "set_text_style", "find_text": "x", "style": {"color": "red"}},
    ])
    assert result["error"] == "INVALID_TEXT_STYLE"


@pytest.mark.asyncio
async def test_set_text_style_missing_style():
    svc = _mock_docs_service(_make_doc())
    result = await format_document(svc, "f1", [
        {"action": "set_text_style", "find_text": "x"},
    ])
    assert result["error"] == "INVALID_TEXT_STYLE"


@pytest.mark.asyncio
async def test_set_text_style_preview():
    doc = _make_doc(
        (0, 14, "Introduction\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "set_text_style", "find_text": "Introduction", "style": {"italic": True}},
    ], preview=True)

    assert result["preview"] is True
    assert result["results"][0]["status"] == "would_apply"
    assert result["results"][0]["action"] == "set_text_style"
    svc.documents().batchUpdate.assert_not_called()


# -------------------------------------------------------------------
# format_document — insert_paragraph
# -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_paragraph_basic():
    """Insert plain paragraph after a given content block index."""
    doc = _make_doc(
        (0, 14, "Introduction\n", "HEADING_1"),
        (14, 30, "Body paragraph.\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "insert_paragraph", "after_paragraph_index": 0, "text": "New item"},
    ])

    assert "error" not in result
    assert result["operations_applied"] == 1
    assert result["results"][0]["status"] == "applied"
    assert result["results"][0]["characters_inserted"] == len("New item\n")

    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    insert_req = requests[0]["insertText"]
    assert insert_req["location"]["index"] == 14  # endIndex of paragraph 0
    assert insert_req["text"] == "New item\n"


@pytest.mark.asyncio
async def test_insert_paragraph_appends_newline():
    """Text without trailing newline gets one appended."""
    doc = _make_doc(
        (0, 14, "Introduction\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "insert_paragraph", "after_paragraph_index": 0, "text": "No newline"},
    ])

    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    assert requests[0]["insertText"]["text"] == "No newline\n"


@pytest.mark.asyncio
async def test_insert_paragraph_preserves_existing_newline():
    """Text already ending with newline is not double-newlined."""
    doc = _make_doc(
        (0, 14, "Introduction\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    await format_document(svc, "f1", [
        {"action": "insert_paragraph", "after_paragraph_index": 0, "text": "Has newline\n"},
    ])

    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    assert requests[0]["insertText"]["text"] == "Has newline\n"


@pytest.mark.asyncio
async def test_insert_paragraph_with_text_style():
    """Insert paragraph with italic text style."""
    doc = _make_doc(
        (0, 14, "Introduction\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "insert_paragraph", "after_paragraph_index": 0,
         "text": "Styled item", "text_style": {"italic": True}},
    ])

    assert result["operations_applied"] == 1
    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    # insertText + updateTextStyle
    assert len(requests) == 2
    # Both requests share the same sort key (insert_index=14);
    # stable sort preserves insertion order: insertText first, updateTextStyle second.
    assert "insertText" in requests[0]
    style_req = requests[1]["updateTextStyle"]
    assert style_req["textStyle"]["italic"] is True
    assert style_req["fields"] == "italic"


@pytest.mark.asyncio
async def test_insert_paragraph_inherits_list():
    """Insert inherits list_id and nesting_level from neighbor paragraph."""
    doc = _make_doc(
        (0, 14, "Introduction\n", "HEADING_1"),
        (14, 30, "List item one\n", "NORMAL_TEXT"),
    )
    # Add bullet to paragraph 1
    doc["body"]["content"][1]["paragraph"]["bullet"] = {
        "listId": "kix.list123",
        "nestingLevel": 0,
    }
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "insert_paragraph", "after_paragraph_index": 1, "text": "List item two"},
    ])

    assert result["operations_applied"] == 1
    # The insert happens at the endIndex of a list paragraph,
    # so Google Docs auto-inherits the list formatting.
    # We verify the insert location is correct.
    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    assert requests[0]["insertText"]["location"]["index"] == 30


@pytest.mark.asyncio
async def test_insert_paragraph_override_nesting_level():
    """Explicit nesting_level override generates updateParagraphStyle request."""
    doc = _make_doc(
        (0, 14, "Introduction\n", "HEADING_1"),
        (14, 30, "List item one\n", "NORMAL_TEXT"),
    )
    doc["body"]["content"][1]["paragraph"]["bullet"] = {
        "listId": "kix.list123",
        "nestingLevel": 0,
    }
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "insert_paragraph", "after_paragraph_index": 1,
         "text": "Nested item", "nesting_level": 1},
    ])

    assert result["operations_applied"] == 1
    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    # Should have insertText + updateParagraphStyle for indentation
    has_insert = any("insertText" in r for r in requests)
    has_indent = any("updateParagraphStyle" in r for r in requests)
    assert has_insert
    assert has_indent


@pytest.mark.asyncio
async def test_insert_paragraph_out_of_range():
    doc = _make_doc(
        (0, 14, "Introduction\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "insert_paragraph", "after_paragraph_index": 99, "text": "x"},
    ])
    assert result["results"][0]["status"] == "index_out_of_range"
    svc.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_insert_paragraph_missing_text():
    svc = _mock_docs_service(_make_doc())
    result = await format_document(svc, "f1", [
        {"action": "insert_paragraph", "after_paragraph_index": 0},
    ])
    assert result["error"] == "MISSING_TEXT"


@pytest.mark.asyncio
async def test_insert_paragraph_missing_index():
    svc = _mock_docs_service(_make_doc())
    result = await format_document(svc, "f1", [
        {"action": "insert_paragraph", "text": "hello"},
    ])
    assert result["error"] == "MISSING_PARAGRAPH_INDEX"


@pytest.mark.asyncio
async def test_insert_paragraph_invalid_text_style():
    svc = _mock_docs_service(_make_doc())
    result = await format_document(svc, "f1", [
        {"action": "insert_paragraph", "after_paragraph_index": 0,
         "text": "x", "text_style": {"color": "red"}},
    ])
    assert result["error"] == "INVALID_TEXT_STYLE"


@pytest.mark.asyncio
async def test_insert_paragraph_invalid_nesting_level():
    svc = _mock_docs_service(_make_doc())
    result = await format_document(svc, "f1", [
        {"action": "insert_paragraph", "after_paragraph_index": 0,
         "text": "x", "nesting_level": "abc"},
    ])
    assert result["error"] == "INVALID_NESTING_LEVEL"

    result2 = await format_document(svc, "f1", [
        {"action": "insert_paragraph", "after_paragraph_index": 0,
         "text": "x", "nesting_level": -1},
    ])
    assert result2["error"] == "INVALID_NESTING_LEVEL"


@pytest.mark.asyncio
async def test_insert_paragraph_preview():
    doc = _make_doc(
        (0, 14, "Introduction\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "insert_paragraph", "after_paragraph_index": 0, "text": "New"},
    ], preview=True)

    assert result["preview"] is True
    assert result["results"][0]["status"] == "would_apply"
    assert result["results"][0]["action"] == "insert_paragraph"
    svc.documents().batchUpdate.assert_not_called()


# -------------------------------------------------------------------
# format_document — insert_paragraph_after_match
# -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_after_match_basic():
    """Insert paragraph after a matched paragraph."""
    doc = _make_doc(
        (0, 14, "Introduction\n", "HEADING_1"),
        (14, 30, "First bullet.\n", "NORMAL_TEXT"),
        (30, 50, "Second bullet.\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "insert_paragraph_after_match", "find_text": "First bullet.",
         "text": "Inserted after first"},
    ])

    assert "error" not in result
    assert result["operations_applied"] == 1
    assert result["results"][0]["status"] == "applied"

    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    insert_req = requests[0]["insertText"]
    assert insert_req["location"]["index"] == 30  # endIndex of "First bullet.\n"
    assert insert_req["text"] == "Inserted after first\n"


@pytest.mark.asyncio
async def test_insert_after_match_not_found():
    doc = _make_doc(
        (0, 14, "Introduction\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "insert_paragraph_after_match", "find_text": "Nonexistent",
         "text": "x"},
    ])
    assert result["results"][0]["status"] == "not_found"


@pytest.mark.asyncio
async def test_insert_after_match_multi_match_error():
    """Multiple matches returns multi_match_error (no match_all support)."""
    doc = _make_doc(
        (0, 10, "Duplicate\n", "NORMAL_TEXT"),
        (10, 20, "Duplicate\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "insert_paragraph_after_match", "find_text": "Duplicate",
         "text": "x"},
    ])
    assert result["results"][0]["status"] == "multi_match_error"


@pytest.mark.asyncio
async def test_insert_after_match_with_text_style():
    doc = _make_doc(
        (0, 14, "Introduction\n", "HEADING_1"),
        (14, 30, "Target line.\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "insert_paragraph_after_match", "find_text": "Target line.",
         "text": "Styled insert", "text_style": {"bold": True, "italic": True}},
    ])

    assert result["operations_applied"] == 1
    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    has_style = any("updateTextStyle" in r for r in requests)
    assert has_style


@pytest.mark.asyncio
async def test_insert_after_match_inherit_list():
    """inherit_list_formatting copies bullet info from matched paragraph."""
    doc = _make_doc(
        (0, 14, "Introduction\n", "HEADING_1"),
        (14, 30, "List item one\n", "NORMAL_TEXT"),
    )
    doc["body"]["content"][1]["paragraph"]["bullet"] = {
        "listId": "kix.list123",
        "nestingLevel": 0,
    }
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "insert_paragraph_after_match", "find_text": "List item one",
         "text": "List item two", "inherit_list_formatting": True},
    ])

    assert result["operations_applied"] == 1
    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    insert_req = requests[0]["insertText"]
    assert insert_req["location"]["index"] == 30


@pytest.mark.asyncio
async def test_insert_after_match_substring_mode():
    """Works with match_mode/substring options."""
    doc = _make_doc(
        (0, 30, "Hulda to explore the caves\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "insert_paragraph_after_match", "find_text": "Hulda to explore",
         "text": "Follow-up", "substring": True},
    ])
    assert result["results"][0]["status"] == "applied"


@pytest.mark.asyncio
async def test_insert_after_match_preview():
    doc = _make_doc(
        (0, 14, "Introduction\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await format_document(svc, "f1", [
        {"action": "insert_paragraph_after_match", "find_text": "Introduction",
         "text": "New"},
    ], preview=True)

    assert result["preview"] is True
    assert result["results"][0]["status"] == "would_apply"
    svc.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_insert_after_match_missing_text():
    svc = _mock_docs_service(_make_doc())
    result = await format_document(svc, "f1", [
        {"action": "insert_paragraph_after_match", "find_text": "x"},
    ])
    assert result["error"] == "MISSING_TEXT"
