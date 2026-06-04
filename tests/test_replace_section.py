"""Tests for _find_heading, _find_section_end, and replace_section in docs_ops."""

import pytest
from unittest.mock import MagicMock, patch

from googleapiclient.errors import HttpError

from gsuite_mcp.docs_ops import (
    _FALLBACK_RANK,
    _HEADING_RANKS,
    _find_heading,
    _find_section_end,
    _para_text,
    replace_section,
)
from gsuite_mcp.server import replace_section as server_replace_section


def _make_doc(*paragraphs):
    """Build a minimal Google Docs body structure.

    Each paragraph is a tuple: (start, end, text, named_style).
    """
    content = []
    for idx, (start, end, text, named_style) in enumerate(paragraphs):
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


# -------------------------------------------------------------------
# _para_text helper
# -------------------------------------------------------------------

def test_para_text_extracts_text():
    para = {
        "elements": [
            {"textRun": {"content": "Hello "}},
            {"textRun": {"content": "World"}},
        ]
    }
    assert _para_text(para) == "Hello World"


def test_para_text_handles_missing_text_run():
    para = {
        "elements": [
            {"inlineObjectElement": {"inlineObjectId": "obj1"}},
            {"textRun": {"content": "text"}},
        ]
    }
    assert _para_text(para) == "text"


# -------------------------------------------------------------------
# _HEADING_RANKS constant
# -------------------------------------------------------------------

def test_heading_ranks_maps_all_levels():
    for level in range(1, 7):
        assert _HEADING_RANKS[f"HEADING_{level}"] == level
    assert _FALLBACK_RANK == 7


# -------------------------------------------------------------------
# _find_heading
# -------------------------------------------------------------------

def test_find_heading_formal_match():
    """Finds a HEADING_1 paragraph by its text."""
    doc = _make_doc(
        (0, 13, "Introduction\n", "HEADING_1"),
        (13, 30, "Some body text.\n", "NORMAL_TEXT"),
    )
    result = _find_heading(doc, "Introduction")
    assert result is not None
    assert result["text"] == "Introduction"
    assert result["heading_level"] == "HEADING_1"
    assert result["level_rank"] == 1
    assert result["start_index"] == 0
    assert result["end_index"] == 13
    assert result["paragraph_index"] == 0


def test_find_heading_case_insensitive():
    """'introduction' matches 'Introduction' (case-insensitive)."""
    doc = _make_doc(
        (0, 13, "Introduction\n", "HEADING_2"),
    )
    result = _find_heading(doc, "introduction")
    assert result is not None
    assert result["text"] == "Introduction"
    assert result["heading_level"] == "HEADING_2"
    assert result["level_rank"] == 2


def test_find_heading_strips_whitespace():
    """'  Introduction  \\n' matches 'Introduction' after stripping."""
    doc = _make_doc(
        (0, 18, "  Introduction  \n", "HEADING_3"),
    )
    result = _find_heading(doc, "Introduction")
    assert result is not None
    assert result["text"] == "Introduction"
    assert result["heading_level"] == "HEADING_3"
    assert result["level_rank"] == 3


def test_find_heading_text_fallback():
    """Matches a NORMAL_TEXT paragraph when no formal heading matches."""
    doc = _make_doc(
        (0, 13, "Introduction\n", "NORMAL_TEXT"),
        (13, 25, "Body text.\n", "NORMAL_TEXT"),
    )
    result = _find_heading(doc, "Introduction")
    assert result is not None
    assert result["text"] == "Introduction"
    assert result["heading_level"] == "NORMAL_TEXT"
    assert result["level_rank"] == _FALLBACK_RANK


def test_find_heading_not_found():
    """Returns None for a heading that does not exist."""
    doc = _make_doc(
        (0, 13, "Introduction\n", "HEADING_1"),
    )
    result = _find_heading(doc, "Conclusion")
    assert result is None


def test_find_heading_ambiguous_returns_none_with_matches():
    """Two 'Summary' headings -> None, and populates matches_out."""
    doc = _make_doc(
        (0, 9, "Summary\n", "HEADING_1"),
        (9, 25, "Some body text.\n", "NORMAL_TEXT"),
        (25, 34, "Summary\n", "HEADING_2"),
    )
    matches_out = []
    result = _find_heading(doc, "Summary", matches_out=matches_out)
    assert result is None
    assert len(matches_out) == 2
    assert matches_out[0]["heading_level"] == "HEADING_1"
    assert matches_out[1]["heading_level"] == "HEADING_2"


def test_find_heading_prefers_formal_over_fallback():
    """When a formal heading and a text fallback both match, only the
    formal heading is considered (pass 1 succeeds, pass 2 is skipped)."""
    doc = _make_doc(
        (0, 13, "Introduction\n", "HEADING_1"),
        (13, 26, "Introduction\n", "NORMAL_TEXT"),
    )
    result = _find_heading(doc, "Introduction")
    assert result is not None
    assert result["heading_level"] == "HEADING_1"
    assert result["level_rank"] == 1


# -------------------------------------------------------------------
# _find_section_end
# -------------------------------------------------------------------

def test_section_end_at_same_level_heading():
    """HEADING_1 section ends at the next HEADING_1."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 30, "Some body text here.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
        (40, 55, "More body text.\n", "NORMAL_TEXT"),
    )
    heading = _find_heading(doc, "Chapter 1")
    assert heading is not None
    end = _find_section_end(doc, heading)
    # Section ends just before the next HEADING_1 starts at index 30
    assert end == 30


def test_section_end_at_higher_level_heading():
    """HEADING_2 section ends when a HEADING_1 is encountered."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 22, "Sub-section\n", "HEADING_2"),
        (22, 40, "Sub-section body.\n", "NORMAL_TEXT"),
        (40, 50, "Chapter 2\n", "HEADING_1"),
    )
    heading = _find_heading(doc, "Sub-section")
    assert heading is not None
    end = _find_section_end(doc, heading)
    # HEADING_1 (rank 1) <= HEADING_2 (rank 2), so section ends at index 40
    assert end == 40


def test_section_end_at_document_end():
    """Last section extends to the end of the document."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 30, "Some body text here.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
        (40, 60, "Final body text....\n", "NORMAL_TEXT"),
    )
    heading = _find_heading(doc, "Chapter 2")
    assert heading is not None
    end = _find_section_end(doc, heading)
    # No heading after Chapter 2, so section extends to end of last block
    assert end == 60


def test_section_end_skips_lower_level_headings():
    """HEADING_1 section spans over HEADING_2 and HEADING_3 subsections."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 25, "Intro paragraph.\n", "NORMAL_TEXT"),
        (25, 40, "Sub-section A\n", "HEADING_2"),
        (40, 55, "Sub-section body\n", "NORMAL_TEXT"),
        (55, 72, "Sub-sub-section\n", "HEADING_3"),
        (72, 90, "Sub-sub body text.\n", "NORMAL_TEXT"),
        (90, 100, "Chapter 2\n", "HEADING_1"),
    )
    heading = _find_heading(doc, "Chapter 1")
    assert heading is not None
    end = _find_section_end(doc, heading)
    # HEADING_2 (rank 2) and HEADING_3 (rank 3) are lower-level (higher rank)
    # than HEADING_1 (rank 1), so they are skipped. Section ends at Chapter 2.
    assert end == 90


def test_section_end_fallback_heading_stops_at_any_formal():
    """A fallback heading (rank 7) section ends at any formal heading."""
    doc = _make_doc(
        (0, 15, "My Bold Title\n", "NORMAL_TEXT"),
        (15, 30, "Some body text.\n", "NORMAL_TEXT"),
        (30, 45, "A Real Heading\n", "HEADING_3"),
        (45, 60, "More body text.\n", "NORMAL_TEXT"),
    )
    heading = _find_heading(doc, "My Bold Title")
    assert heading is not None
    assert heading["level_rank"] == _FALLBACK_RANK
    end = _find_section_end(doc, heading)
    # Any formal heading (HEADING_3, rank 3) terminates a fallback section (rank 7)
    assert end == 30


# -------------------------------------------------------------------
# replace_section
# -------------------------------------------------------------------

def _mock_docs_service(doc, batch_response=None):
    svc = MagicMock()
    svc.documents().get.return_value.execute.return_value = doc
    if batch_response is None:
        batch_response = {"replies": []}
    svc.documents().batchUpdate.return_value.execute.return_value = batch_response
    return svc


@pytest.mark.asyncio
async def test_replace_section_basic():
    """Replace body text preserving the heading."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 30, "Old body text here.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(svc, "file123", "Chapter 1", "New body.\n")

    assert result["file_id"] == "file123"
    assert result["section_heading"] == "Chapter 1"
    assert result["heading_level"] == "HEADING_1"
    assert result["characters_deleted"] == 30 - 10  # heading end to section end
    assert result["characters_inserted"] == len("New body.\n")
    assert result["include_heading"] is False

    # Verify batchUpdate was called
    call_args = svc.documents().batchUpdate.call_args
    body = call_args[1]["body"] if "body" in (call_args[1] or {}) else call_args[0][0] if call_args[0] else call_args[1].get("body")
    requests = body["requests"]

    # First request: deleteContentRange [heading_end, section_end)
    delete_req = requests[0]["deleteContentRange"]["range"]
    assert delete_req["startIndex"] == 10
    assert delete_req["endIndex"] == 30

    # Second request: insertText at delete_start
    insert_req = requests[1]["insertText"]
    assert insert_req["location"]["index"] == 10
    assert insert_req["text"] == "New body.\n"

    # Third request: updateParagraphStyle NORMAL_TEXT
    style_req = requests[2]["updateParagraphStyle"]
    assert style_req["paragraphStyle"]["namedStyleType"] == "NORMAL_TEXT"
    assert style_req["fields"] == "namedStyleType"


@pytest.mark.asyncio
async def test_replace_section_include_heading():
    """include_heading=True deletes from heading start and restores heading style."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 30, "Old body text here.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(
        svc, "file123", "Chapter 1", "New Heading\nNew body.\n",
        include_heading=True,
    )

    assert result["include_heading"] is True
    assert result["characters_deleted"] == 30 - 0  # heading start to section end

    call_args = svc.documents().batchUpdate.call_args
    body = call_args[1]["body"] if "body" in (call_args[1] or {}) else call_args[0][0] if call_args[0] else call_args[1].get("body")
    requests = body["requests"]

    # Delete range starts at heading start (0)
    delete_req = requests[0]["deleteContentRange"]["range"]
    assert delete_req["startIndex"] == 0
    assert delete_req["endIndex"] == 30

    # Insert at heading start
    insert_req = requests[1]["insertText"]
    assert insert_req["location"]["index"] == 0

    # Should have NORMAL_TEXT style AND heading style for first paragraph
    # Find the heading style request (restores original HEADING_1)
    heading_style_found = False
    for req in requests:
        if "updateParagraphStyle" in req:
            ps = req["updateParagraphStyle"]
            if ps["paragraphStyle"]["namedStyleType"] == "HEADING_1":
                heading_style_found = True
    assert heading_style_found


@pytest.mark.asyncio
async def test_replace_section_heading_not_found():
    """Returns HEADING_NOT_FOUND when heading doesn't exist."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(svc, "file123", "Nonexistent", "text")

    assert result["error"] == "HEADING_NOT_FOUND"
    assert result["retryable"] is False
    assert "message" in result


@pytest.mark.asyncio
async def test_replace_section_ambiguous():
    """Returns AMBIGUOUS_HEADING with matches list when multiple headings match."""
    doc = _make_doc(
        (0, 9, "Summary\n", "HEADING_1"),
        (9, 25, "Some body text.\n", "NORMAL_TEXT"),
        (25, 34, "Summary\n", "HEADING_2"),
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(svc, "file123", "Summary", "text")

    assert result["error"] == "AMBIGUOUS_HEADING"
    assert result["retryable"] is False
    assert "matches" in result
    assert len(result["matches"]) == 2
    for m in result["matches"]:
        assert "text" in m
        assert "start_index" in m
        assert "heading_level" in m


@pytest.mark.asyncio
async def test_replace_section_last_section_extends_to_end():
    """Section at end of doc extends to end, replacement works."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 30, "Some body text here.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
        (40, 60, "Final body text....\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(svc, "file123", "Chapter 2", "Replaced.\n")

    assert result["file_id"] == "file123"
    assert result["characters_deleted"] == 60 - 40  # heading end to doc end
    assert result["characters_inserted"] == len("Replaced.\n")


@pytest.mark.asyncio
async def test_replace_section_applies_normal_text_style():
    """Verify NORMAL_TEXT style is applied to the inserted content range."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 30, "Old body text here.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    await replace_section(svc, "file123", "Chapter 1", "New text.\n")

    call_args = svc.documents().batchUpdate.call_args
    body = call_args[1]["body"] if "body" in (call_args[1] or {}) else call_args[0][0] if call_args[0] else call_args[1].get("body")
    requests = body["requests"]

    normal_style_requests = [
        r for r in requests
        if "updateParagraphStyle" in r
        and r["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"] == "NORMAL_TEXT"
    ]
    assert len(normal_style_requests) >= 1
    for r in normal_style_requests:
        assert r["updateParagraphStyle"]["fields"] == "namedStyleType"


@pytest.mark.asyncio
async def test_replace_section_include_heading_applies_heading_style():
    """When include_heading=True, original heading style applied to first paragraph."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_2"),
        (10, 30, "Old body text here.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    await replace_section(
        svc, "file123", "Chapter 1", "New Title\nNew body.\n",
        include_heading=True,
    )

    call_args = svc.documents().batchUpdate.call_args
    body = call_args[1]["body"] if "body" in (call_args[1] or {}) else call_args[0][0] if call_args[0] else call_args[1].get("body")
    requests = body["requests"]

    # Find the heading style request
    heading_style_reqs = [
        r for r in requests
        if "updateParagraphStyle" in r
        and r["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"] == "HEADING_2"
    ]
    assert len(heading_style_reqs) == 1
    req = heading_style_reqs[0]["updateParagraphStyle"]
    # Should cover first line only: from delete_start (heading start=0) to first newline
    assert req["range"]["startIndex"] == 0
    first_newline = "New Title\n".index("\n") + 1
    assert req["range"]["endIndex"] == 0 + first_newline
    assert req["fields"] == "namedStyleType"


@pytest.mark.asyncio
async def test_replace_section_empty_section_body():
    """Heading immediately followed by same-level heading -> insert after heading."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 20, "Chapter 2\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(svc, "file123", "Chapter 1", "New text.\n")

    assert "error" not in result
    assert result["characters_deleted"] == 0
    assert result["characters_inserted"] == len("New text.\n")


@pytest.mark.asyncio
async def test_replace_section_empty_section_inserts_after_heading():
    """Empty section body with include_heading=False inserts after heading."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 20, "Chapter 2\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(svc, "file123", "Chapter 1", "New body text.\n")

    assert "error" not in result
    assert result["characters_deleted"] == 0
    assert result["characters_inserted"] == len("New body text.\n")

    # Verify batchUpdate was called (insert + style, no delete)
    call_args = svc.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    actions = [list(r.keys())[0] for r in requests]
    assert "deleteContentRange" not in actions
    assert "insertText" in actions
    assert "updateParagraphStyle" in actions
    insert_req = [r for r in requests if "insertText" in r][0]
    assert insert_req["insertText"]["location"]["index"] == 10


@pytest.mark.asyncio
async def test_replace_section_empty_last_section_inserts():
    """Last section with no body content inserts after heading."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 30, "Some body.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(svc, "file123", "Chapter 2", "Final content.\n")

    assert "error" not in result
    assert result["characters_deleted"] == 0
    assert result["characters_inserted"] == len("Final content.\n")


@pytest.mark.asyncio
async def test_replace_section_final_section_clamps_trailing_newline():
    """Replacing the final section clamps delete endIndex to preserve trailing newline."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 30, "Some body text here.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
        (40, 60, "Final body text....\n", "NORMAL_TEXT"),  # 60 = doc end
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(svc, "file123", "Chapter 2", "New final.\n")

    assert "error" not in result
    assert result["characters_deleted"] == 60 - 40  # logical deletion

    call_args = svc.documents().batchUpdate.call_args
    body = call_args[1]["body"] if "body" in (call_args[1] or {}) else call_args[0][0] if call_args[0] else call_args[1].get("body")
    requests = body["requests"]

    # The delete request endIndex must be clamped (60 - 1 = 59)
    delete_req = requests[0]["deleteContentRange"]["range"]
    assert delete_req["startIndex"] == 40
    assert delete_req["endIndex"] == 59  # Clamped!


@pytest.mark.asyncio
async def test_replace_section_ensures_trailing_newline():
    """Content without trailing newline gets one added."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 30, "Old body text here.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(svc, "file123", "Chapter 1", "No newline")

    # characters_inserted should include the added newline
    assert result["characters_inserted"] == len("No newline\n")

    call_args = svc.documents().batchUpdate.call_args
    body = call_args[1]["body"] if "body" in (call_args[1] or {}) else call_args[0][0] if call_args[0] else call_args[1].get("body")
    requests = body["requests"]

    insert_req = requests[1]["insertText"]
    assert insert_req["text"] == "No newline\n"


# -------------------------------------------------------------------
# Server-level tests for replace_section tool wrapper
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


def _make_http_error(status: int) -> HttpError:
    resp = MagicMock()
    resp.status = status
    return HttpError(resp, b"error")


# -------------------------------------------------------------------
# dry_run, expected_delete_chars, anchor flags, blast-radius guard
# -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replace_section_dry_run_returns_span():
    """dry_run=True returns computed span without calling batchUpdate."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 30, "Old body text here.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(
        svc, "file123", "Chapter 1", "New body.\n", dry_run=True,
    )
    assert "error" not in result
    assert result["dry_run"] is True
    assert result["chars_deleted"] == 20  # 30 - 10
    assert result["chars_inserted"] == len("New body.\n")
    assert result["section_span"] == {"start_index": 10, "end_index": 30}
    svc.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_replace_section_dry_run_include_heading():
    """dry_run with include_heading shows correct span from heading start."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 30, "Old body text here.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(
        svc, "file123", "Chapter 1", "New.\n",
        include_heading=True, dry_run=True,
    )
    assert result["dry_run"] is True
    assert result["chars_deleted"] == 30  # 0 to 30
    assert result["section_span"] == {"start_index": 0, "end_index": 30}
    svc.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_replace_section_expected_delete_chars_match():
    """expected_delete_chars matching computed value proceeds normally."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 30, "Old body text here.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(
        svc, "file123", "Chapter 1", "New.\n",
        expected_delete_chars=20,
    )
    assert "error" not in result
    assert result["characters_deleted"] == 20


@pytest.mark.asyncio
async def test_replace_section_expected_delete_chars_mismatch():
    """expected_delete_chars not matching aborts with DELETE_CHARS_MISMATCH."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 30, "Old body text here.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(
        svc, "file123", "Chapter 1", "New.\n",
        expected_delete_chars=999,
    )
    assert result["error"] == "DELETE_CHARS_MISMATCH"
    assert result["expected"] == 999
    assert result["actual"] == 20
    assert result["retryable"] is True
    svc.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_replace_section_normal_text_anchor_flagged():
    """NORMAL_TEXT anchor includes anchor_is_styled_heading=false and section_extends_to."""
    doc = _make_doc(
        (0, 15, "My Bold Title\n", "NORMAL_TEXT"),
        (15, 30, "Some body text.\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(svc, "file123", "My Bold Title", "New.\n")
    assert "error" not in result
    assert result["anchor_is_styled_heading"] is False
    assert result["section_extends_to"] == "END_OF_DOCUMENT"


@pytest.mark.asyncio
async def test_replace_section_formal_heading_anchor_flagged():
    """Formal heading includes anchor_is_styled_heading=true."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 30, "Old body text here.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(svc, "file123", "Chapter 1", "New.\n")
    assert "error" not in result
    assert result["anchor_is_styled_heading"] is True
    assert "section_extends_to" not in result or result.get("section_extends_to") != "END_OF_DOCUMENT"


@pytest.mark.asyncio
async def test_replace_section_dry_run_includes_anchor_flags():
    """dry_run response also includes anchor_is_styled_heading."""
    doc = _make_doc(
        (0, 15, "My Bold Title\n", "NORMAL_TEXT"),
        (15, 30, "Some body text.\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(
        svc, "file123", "My Bold Title", "New.\n", dry_run=True,
    )
    assert result["anchor_is_styled_heading"] is False
    assert result["section_extends_to"] == "END_OF_DOCUMENT"


@pytest.mark.asyncio
async def test_replace_section_blast_radius_trips():
    """Large deletion without confirm trips blast-radius guard."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 6663, "x" * 6652 + "\n", "NORMAL_TEXT"),
        (6663, 6673, "Chapter 2\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(
        svc, "file123", "Chapter 1", "y" * 200 + "\n",
    )
    assert result["error"] == "BLAST_RADIUS_EXCEEDED"
    assert result["chars_deleted"] == 6653
    assert result["chars_inserted"] == 201
    assert result["retryable"] is True
    svc.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_replace_section_blast_radius_confirmed():
    """Passing confirm_delete_chars bypasses the guard."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 6663, "x" * 6652 + "\n", "NORMAL_TEXT"),
        (6663, 6673, "Chapter 2\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(
        svc, "file123", "Chapter 1", "y" * 200 + "\n",
        confirm_delete_chars=6653,
    )
    assert "error" not in result
    assert result["characters_deleted"] == 6653
    svc.documents().batchUpdate.assert_called_once()


@pytest.mark.asyncio
async def test_replace_section_blast_radius_skipped_on_dry_run():
    """dry_run does NOT trip blast-radius guard (just returns the info)."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 6663, "x" * 6652 + "\n", "NORMAL_TEXT"),
        (6663, 6673, "Chapter 2\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(
        svc, "file123", "Chapter 1", "y" * 200 + "\n",
        dry_run=True,
    )
    assert result.get("dry_run") is True
    assert result["chars_deleted"] == 6653
    assert "error" not in result


@pytest.mark.asyncio
async def test_replace_section_returns_section_span():
    """Normal (non-dry-run) result includes section_span."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 30, "Old body text here.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(svc, "file123", "Chapter 1", "New body.\n")
    assert result["section_span"] == {"start_index": 10, "end_index": 30}
    assert result["anchor_is_styled_heading"] is True


# -------------------------------------------------------------------
# Server-level tests for replace_section tool wrapper
# -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_server_replace_section_not_a_google_doc(mock_services):
    """Server wrapper rejects non-Google-Doc files with NOT_A_GOOGLE_DOC."""
    drive = mock_services["drive"]
    drive.files().get.return_value.execute.return_value = {
        "name": "file.docx",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "modifiedTime": "2026-05-10T12:00:00Z",
    }

    result = await server_replace_section(
        file_id="d1",
        section_heading="Chapter 1",
        new_content="New body.\n",
    )

    assert result["error"] == "NOT_A_GOOGLE_DOC"
    assert result["retryable"] is False
    assert "replace_section" in result["message"]


@pytest.mark.asyncio
async def test_server_replace_section_success(mock_services):
    """Server wrapper returns result with modified_time on success."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    # Drive metadata says it's a Google Doc
    drive.files().get.return_value.execute.return_value = {
        "name": "My Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-05-10T14:00:00Z",
    }

    # Docs API returns the document structure
    docs.documents().get.return_value.execute.return_value = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 30, "Old body text here.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
    )
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    result = await server_replace_section(
        file_id="d1",
        section_heading="Chapter 1",
        new_content="New body.\n",
    )

    assert "error" not in result
    assert result["file_id"] == "d1"
    assert result["section_heading"] == "Chapter 1"
    assert "modified_time" in result
    assert result["modified_time"] == "2026-05-10T14:00:00Z"


@pytest.mark.asyncio
async def test_server_replace_section_catches_http_error(mock_services):
    """Server wrapper catches HttpError 500 and returns structured error."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().get.return_value.execute.return_value = {
        "name": "My Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-05-10T14:00:00Z",
    }

    # Docs API document fetch raises HttpError
    docs.documents().get.return_value.execute.side_effect = _make_http_error(500)

    result = await server_replace_section(
        file_id="d1",
        section_heading="Chapter 1",
        new_content="New body.\n",
    )

    assert result["error"] == "GOOGLE_API_ERROR"
    assert result["retryable"] is True
    assert result["http_status"] == 500
    assert "500" in result["message"]
