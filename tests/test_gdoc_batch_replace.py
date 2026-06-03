"""Tests for gdoc_batch_replace tool."""

from unittest.mock import MagicMock, patch
import pytest


def _make_doc(*paragraphs: str) -> dict:
    """Build a minimal Google Docs document structure from paragraph strings.

    Each paragraph gets a trailing newline (matching real Docs API behavior).
    Indices are 1-based (index 0 is reserved by the API).
    """
    content = []
    idx = 1  # Docs API body starts at index 1
    for text in paragraphs:
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


# ---- docs_ops.batch_replace unit tests ----


@pytest.mark.asyncio
async def test_batch_replace_happy_path():
    """Two pairs, both found once, committed."""
    docs = MagicMock()
    doc = _make_doc("Hello world", "Goodbye world")
    docs.documents().get.return_value.execute.return_value = doc
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    from gsuite_mcp.docs_ops import batch_replace
    result = await batch_replace(
        docs, "file1",
        edits=[
            {"find_text": "Hello", "replace_text": "Hi"},
            {"find_text": "Goodbye", "replace_text": "Bye"},
        ],
    )

    assert result["committed"] is True
    assert result["total_replacements"] == 2
    assert len(result["results"]) == 2
    assert result["results"][0]["matches_found"] == 1
    assert result["results"][1]["matches_found"] == 1
    # Verify batchUpdate was called
    docs.documents().batchUpdate.assert_called_once()


@pytest.mark.asyncio
async def test_batch_replace_dry_run():
    """dry_run=True returns counts without calling batchUpdate."""
    docs = MagicMock()
    doc = _make_doc("Hello world Hello again")
    docs.documents().get.return_value.execute.return_value = doc

    from gsuite_mcp.docs_ops import batch_replace
    result = await batch_replace(
        docs, "file1",
        edits=[{"find_text": "Hello", "replace_text": "Hi"}],
        dry_run=True,
    )

    assert result["committed"] is False
    assert result["results"][0]["matches_found"] == 2
    docs.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_batch_replace_count_mismatch_aborts():
    """If any pair has expected_count mismatch, entire batch aborts."""
    docs = MagicMock()
    doc = _make_doc("Hello Hello Hello")  # 3 occurrences
    docs.documents().get.return_value.execute.return_value = doc

    from gsuite_mcp.docs_ops import batch_replace
    result = await batch_replace(
        docs, "file1",
        edits=[{"find_text": "Hello", "replace_text": "Hi", "expected_count": 1}],
    )

    assert result["committed"] is False
    assert result["results"][0]["status"] == "count_mismatch"
    assert result["results"][0]["matches_found"] == 3
    assert result["results"][0]["expected_count"] == 1
    docs.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_batch_replace_expected_count_ok():
    """expected_count matching actual count proceeds normally."""
    docs = MagicMock()
    doc = _make_doc("Hello world")
    docs.documents().get.return_value.execute.return_value = doc
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    from gsuite_mcp.docs_ops import batch_replace
    result = await batch_replace(
        docs, "file1",
        edits=[{"find_text": "Hello", "replace_text": "Hi", "expected_count": 1}],
    )

    assert result["committed"] is True
    assert result["results"][0]["status"] == "ok"


@pytest.mark.asyncio
async def test_batch_replace_cross_paragraph():
    """Find text spanning a paragraph break (includes newline)."""
    docs = MagicMock()
    # "world\nGoodbye" spans the paragraph break
    doc = _make_doc("Hello world", "Goodbye friend")
    docs.documents().get.return_value.execute.return_value = doc
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    from gsuite_mcp.docs_ops import batch_replace
    result = await batch_replace(
        docs, "file1",
        edits=[{"find_text": "world\nGoodbye", "replace_text": "everyone"}],
    )

    assert result["committed"] is True
    assert result["results"][0]["matches_found"] == 1


@pytest.mark.asyncio
async def test_batch_replace_overlapping_matches():
    """Overlapping match regions across pairs should abort."""
    docs = MagicMock()
    doc = _make_doc("Hello world")
    docs.documents().get.return_value.execute.return_value = doc

    from gsuite_mcp.docs_ops import batch_replace
    result = await batch_replace(
        docs, "file1",
        edits=[
            {"find_text": "Hello world", "replace_text": "Hi"},
            {"find_text": "world", "replace_text": "earth"},
        ],
    )

    assert result["committed"] is False
    assert "error" in result
    assert result["error"] == "OVERLAPPING_MATCHES"
    docs.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_batch_replace_zero_matches_no_error():
    """A pair with 0 matches and no expected_count is a no-op, not an error."""
    docs = MagicMock()
    doc = _make_doc("Hello world")
    docs.documents().get.return_value.execute.return_value = doc
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    from gsuite_mcp.docs_ops import batch_replace
    result = await batch_replace(
        docs, "file1",
        edits=[
            {"find_text": "Hello", "replace_text": "Hi"},
            {"find_text": "NOTFOUND", "replace_text": "x"},
        ],
    )

    assert result["committed"] is True
    assert result["total_replacements"] == 1
    assert result["results"][0]["matches_found"] == 1
    assert result["results"][1]["matches_found"] == 0


@pytest.mark.asyncio
async def test_batch_replace_all_zero_matches_no_commit():
    """If every pair matches 0 times, skip batchUpdate entirely."""
    docs = MagicMock()
    doc = _make_doc("Hello world")
    docs.documents().get.return_value.execute.return_value = doc

    from gsuite_mcp.docs_ops import batch_replace
    result = await batch_replace(
        docs, "file1",
        edits=[{"find_text": "NOTFOUND", "replace_text": "x"}],
    )

    assert result["committed"] is False
    assert result["total_replacements"] == 0
    docs.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_batch_replace_reverse_order_requests():
    """Verify delete+insert requests are built in reverse document order."""
    docs = MagicMock()
    doc = _make_doc("AAA BBB AAA")
    docs.documents().get.return_value.execute.return_value = doc
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    from gsuite_mcp.docs_ops import batch_replace
    await batch_replace(
        docs, "file1",
        edits=[{"find_text": "AAA", "replace_text": "X"}],
    )

    call_args = docs.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    # Should have 4 requests: delete+insert for each of 2 matches
    assert len(requests) == 4
    # First request pair should be for the LATER match (higher index)
    first_delete = requests[0]["deleteContentRange"]["range"]
    second_delete = requests[2]["deleteContentRange"]["range"]
    assert first_delete["startIndex"] > second_delete["startIndex"]


# ---- gdoc_ops.batch_replace wrapper tests ----


@pytest.mark.asyncio
async def test_gdoc_ops_batch_replace_includes_revision_ids():
    """Wrapper enriches result with Drive revision IDs."""
    drive = MagicMock()
    docs = MagicMock()

    doc = _make_doc("Hello world")
    docs.documents().get.return_value.execute.return_value = doc
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    # Mock revisions.list — called twice (before and after)
    revisions_mock = MagicMock()
    call_count = 0

    def revisions_execute():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"revisions": [{"id": "rev_before"}]}
        return {"revisions": [{"id": "rev_after"}]}

    revisions_mock.execute = revisions_execute
    drive.revisions().list.return_value = revisions_mock

    from gsuite_mcp.gdoc_ops import batch_replace
    result = await batch_replace(
        drive, docs, "file1",
        edits=[{"find_text": "Hello", "replace_text": "Hi"}],
    )

    assert result["committed"] is True
    assert result["revision_id_before"] == "rev_before"
    assert result["revision_id_after"] == "rev_after"


@pytest.mark.asyncio
async def test_gdoc_ops_batch_replace_dry_run_skips_after_revision():
    """Dry run fetches before revision but not after."""
    drive = MagicMock()
    docs = MagicMock()

    doc = _make_doc("Hello world")
    docs.documents().get.return_value.execute.return_value = doc

    drive.revisions().list.return_value.execute.return_value = {
        "revisions": [{"id": "rev_before"}]
    }

    from gsuite_mcp.gdoc_ops import batch_replace
    result = await batch_replace(
        drive, docs, "file1",
        edits=[{"find_text": "Hello", "replace_text": "Hi"}],
        dry_run=True,
    )

    assert result["committed"] is False
    assert result["revision_id_before"] == "rev_before"
    assert "revision_id_after" not in result
    # revisions.list called only once
    assert drive.revisions().list.call_count == 1


@pytest.mark.asyncio
async def test_gdoc_ops_batch_replace_aborted_skips_after_revision():
    """Count mismatch abort fetches before revision but not after."""
    drive = MagicMock()
    docs = MagicMock()

    doc = _make_doc("Hello Hello Hello")
    docs.documents().get.return_value.execute.return_value = doc

    drive.revisions().list.return_value.execute.return_value = {
        "revisions": [{"id": "rev_before"}]
    }

    from gsuite_mcp.gdoc_ops import batch_replace
    result = await batch_replace(
        drive, docs, "file1",
        edits=[{"find_text": "Hello", "replace_text": "Hi", "expected_count": 1}],
    )

    assert result["committed"] is False
    assert result["revision_id_before"] == "rev_before"
    assert "revision_id_after" not in result


# ---- server.gdoc_batch_replace tool tests ----


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
async def test_tool_trashed_file_refused(mock_services):
    drive = mock_services["drive"]
    drive.files().get.return_value.execute.return_value = {
        "name": "Doc", "mimeType": "application/vnd.google-apps.document",
        "trashed": True, "trashedTime": "2026-01-01T00:00:00Z",
    }

    from gsuite_mcp.server import gdoc_batch_replace
    result = await gdoc_batch_replace(
        file_id="f1",
        edits=[{"find_text": "a", "replace_text": "b"}],
    )
    assert result["error"] == "TRASHED_FILE"


@pytest.mark.asyncio
async def test_tool_not_a_google_doc(mock_services):
    drive = mock_services["drive"]
    drive.files().get.return_value.execute.return_value = {
        "name": "file.docx",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }

    from gsuite_mcp.server import gdoc_batch_replace
    result = await gdoc_batch_replace(
        file_id="f1",
        edits=[{"find_text": "a", "replace_text": "b"}],
    )
    assert result["error"] == "NOT_A_GOOGLE_DOC"


@pytest.mark.asyncio
async def test_tool_review_doc_blocked(mock_services):
    drive = mock_services["drive"]
    drive.files().get.return_value.execute.return_value = {
        "name": "Career Strategy",
        "mimeType": "application/vnd.google-apps.document",
    }

    with patch.dict("os.environ", {"GDOC_REVIEW_DOC_IDS": "review1,review2,review3"}):
        from gsuite_mcp.server import gdoc_batch_replace
        result = await gdoc_batch_replace(
            file_id="review2",
            edits=[{"find_text": "a", "replace_text": "b"}],
        )
    assert result["error"] == "REVIEW_DOC_BLOCKED"


@pytest.mark.asyncio
async def test_tool_review_doc_allowed_with_flag(mock_services):
    drive = mock_services["drive"]
    docs = mock_services["docs"]
    drive.files().get.return_value.execute.return_value = {
        "name": "Career Strategy",
        "mimeType": "application/vnd.google-apps.document",
    }
    doc = _make_doc("Hello world")
    docs.documents().get.return_value.execute.return_value = doc
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}
    drive.revisions().list.return_value.execute.return_value = {
        "revisions": [{"id": "r1"}]
    }

    with patch.dict("os.environ", {"GDOC_REVIEW_DOC_IDS": "review1,review2"}):
        from gsuite_mcp.server import gdoc_batch_replace
        result = await gdoc_batch_replace(
            file_id="review1",
            edits=[{"find_text": "Hello", "replace_text": "Hi"}],
            allow_review_docs=True,
        )
    assert result["committed"] is True


@pytest.mark.asyncio
async def test_tool_review_doc_blocked_on_dry_run(mock_services):
    """Denylist guard fires on dry_run too, not just commit path."""
    drive = mock_services["drive"]
    drive.files().get.return_value.execute.return_value = {
        "name": "Career Strategy",
        "mimeType": "application/vnd.google-apps.document",
    }

    with patch.dict("os.environ", {"GDOC_REVIEW_DOC_IDS": "review1,review2"}):
        from gsuite_mcp.server import gdoc_batch_replace
        result = await gdoc_batch_replace(
            file_id="review1",
            edits=[{"find_text": "a", "replace_text": "b"}],
            dry_run=True,
        )
    assert result["error"] == "REVIEW_DOC_BLOCKED"


@pytest.mark.asyncio
async def test_tool_refuses_when_denylist_env_unset(mock_services):
    """gdoc_batch_replace must fail-closed when GDOC_REVIEW_DOC_IDS is not set."""
    drive = mock_services["drive"]
    drive.files().get.return_value.execute.return_value = {
        "name": "Doc",
        "mimeType": "application/vnd.google-apps.document",
    }

    with patch.dict("os.environ", {}, clear=True):
        from gsuite_mcp.server import gdoc_batch_replace
        result = await gdoc_batch_replace(
            file_id="f1",
            edits=[{"find_text": "a", "replace_text": "b"}],
        )
    assert result["error"] == "DENYLIST_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_tool_refuses_when_denylist_env_empty(mock_services):
    """Empty GDOC_REVIEW_DOC_IDS is as dangerous as unset — refuse."""
    drive = mock_services["drive"]
    drive.files().get.return_value.execute.return_value = {
        "name": "Doc",
        "mimeType": "application/vnd.google-apps.document",
    }

    with patch.dict("os.environ", {"GDOC_REVIEW_DOC_IDS": ""}):
        from gsuite_mcp.server import gdoc_batch_replace
        result = await gdoc_batch_replace(
            file_id="f1",
            edits=[{"find_text": "a", "replace_text": "b"}],
        )
    assert result["error"] == "DENYLIST_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_tool_empty_edits_rejected(mock_services):
    drive = mock_services["drive"]
    drive.files().get.return_value.execute.return_value = {
        "name": "Doc",
        "mimeType": "application/vnd.google-apps.document",
    }

    from gsuite_mcp.server import gdoc_batch_replace
    result = await gdoc_batch_replace(file_id="f1", edits=[])
    assert result["error"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_tool_missing_fields_rejected(mock_services):
    drive = mock_services["drive"]
    drive.files().get.return_value.execute.return_value = {
        "name": "Doc",
        "mimeType": "application/vnd.google-apps.document",
    }

    from gsuite_mcp.server import gdoc_batch_replace
    result = await gdoc_batch_replace(
        file_id="f1",
        edits=[{"find_text": "a"}],  # missing replace_text
    )
    assert result["error"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_tool_happy_path_with_modified_time(mock_services):
    """Full integration: tool returns file_id, committed, modified_time."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]
    drive.files().get.return_value.execute.return_value = {
        "name": "Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-06-01T00:00:00Z",
    }
    doc = _make_doc("Hello world")
    docs.documents().get.return_value.execute.return_value = doc
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}
    drive.revisions().list.return_value.execute.return_value = {
        "revisions": [{"id": "r1"}]
    }

    from gsuite_mcp.server import gdoc_batch_replace
    result = await gdoc_batch_replace(
        file_id="f1",
        edits=[{"find_text": "Hello", "replace_text": "Hi"}],
    )
    assert result["file_id"] == "f1"
    assert result["committed"] is True
    assert "modified_time" in result
