# `gdoc_batch_replace` Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a `gdoc_batch_replace` MCP tool that edits a live Google Doc in place with an array of find/replace pairs, count verification, dry-run mode, and a review-doc denylist guard.

**Architecture:** Client-side batch replacement using `deleteContentRange` + `insertText` in a single Docs API `batchUpdate`. Three layers: `docs_ops.batch_replace()` (core logic), `gdoc_ops.batch_replace()` (revision ID enrichment), `server.gdoc_batch_replace` (guards + tool registration). Reuses existing `_flatten_doc_text` and `_find_all_substring` helpers.

**Tech Stack:** Python 3.12, Google Docs API v1, Google Drive API v3, FastMCP, pytest

---

### Task 1: `docs_ops.batch_replace()` — core logic with tests

**Files:**
- Modify: `src/gsuite_mcp/docs_ops.py` (add `batch_replace` function at end of file)
- Create: `tests/test_gdoc_batch_replace.py`

**Step 1: Write failing tests for `docs_ops.batch_replace`**

Create `tests/test_gdoc_batch_replace.py` with these tests. All tests mock `docs_service` the same way existing tests do (MagicMock with chained `.documents().get()` and `.documents().batchUpdate()`).

Build a helper to create a fake Google Docs document structure:

```python
"""Tests for gdoc_batch_replace tool."""

from unittest.mock import patch, MagicMock, AsyncMock
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
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gdoc_batch_replace.py -v`
Expected: All tests FAIL with `ImportError: cannot import name 'batch_replace' from 'gsuite_mcp.docs_ops'`

**Step 3: Implement `docs_ops.batch_replace()`**

Add to the end of `src/gsuite_mcp/docs_ops.py`:

```python
# ---------------------------------------------------------------------------
# Batch replace
# ---------------------------------------------------------------------------


async def batch_replace(
    docs_service,
    file_id: str,
    edits: list[dict],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Batch find/replace across a Google Doc using client-side matching.

    Supports cross-paragraph matches. All pairs are applied in a single
    batchUpdate for atomicity. Match regions must not overlap across pairs.

    Each edit dict must have ``find_text`` and ``replace_text``.
    Optional ``expected_count`` aborts the entire batch on mismatch.
    """
    doc = await asyncio.to_thread(
        lambda: docs_service.documents()
        .get(documentId=file_id)
        .execute()
    )
    flat, index_map = _flatten_doc_text(doc)

    # Phase 1: find all matches for every pair
    all_pair_matches: list[list[tuple[int, int]]] = []
    results: list[dict[str, Any]] = []
    has_count_mismatch = False

    for edit in edits:
        find_text = edit["find_text"]
        replace_text = edit["replace_text"]
        expected_count = edit.get("expected_count")

        matches = _find_all_substring(flat, find_text, match_case=True)
        status = "ok"

        if expected_count is not None and len(matches) != expected_count:
            status = "count_mismatch"
            has_count_mismatch = True

        results.append({
            "find_text": find_text,
            "matches_found": len(matches),
            "expected_count": expected_count,
            "status": status,
        })
        all_pair_matches.append(matches)

    # Abort on any count mismatch
    if has_count_mismatch:
        return {
            "results": results,
            "committed": False,
            "total_replacements": 0,
        }

    # Phase 2: check for overlapping match regions across pairs
    all_regions: list[tuple[int, int, int]] = []  # (start, end, pair_index)
    for pair_idx, matches in enumerate(all_pair_matches):
        for start, end in matches:
            all_regions.append((start, end, pair_idx))
    all_regions.sort()

    for i in range(len(all_regions) - 1):
        _, end_a, pair_a = all_regions[i]
        start_b, _, pair_b = all_regions[i + 1]
        if pair_a != pair_b and end_a > start_b:
            return {
                "error": "OVERLAPPING_MATCHES",
                "retryable": False,
                "message": (
                    f"Match regions overlap between pair {pair_a} "
                    f"({edits[pair_a]['find_text']!r}) and pair {pair_b} "
                    f"({edits[pair_b]['find_text']!r}). "
                    f"Split into separate calls."
                ),
                "results": results,
                "committed": False,
                "total_replacements": 0,
            }

    # Count total replacements
    total = sum(len(m) for m in all_pair_matches)

    if dry_run or total == 0:
        return {
            "results": results,
            "committed": False,
            "total_replacements": total if not dry_run else 0,
            "dry_run": dry_run,
        }

    # Phase 3: build requests in reverse document order
    # Collect (abs_start, abs_end, replace_text) for all matches
    ops: list[tuple[int, int, str]] = []
    for pair_idx, matches in enumerate(all_pair_matches):
        replace_text = edits[pair_idx]["replace_text"]
        for m_start, m_end in matches:
            abs_start = index_map[m_start]
            abs_end = index_map[m_end - 1] + 1
            ops.append((abs_start, abs_end, replace_text))

    # Sort by start position descending (reverse doc order)
    ops.sort(key=lambda x: x[0], reverse=True)

    requests: list[dict] = []
    for abs_start, abs_end, replacement in ops:
        requests.append({
            "deleteContentRange": {
                "range": {"startIndex": abs_start, "endIndex": abs_end}
            }
        })
        requests.append({
            "insertText": {
                "location": {"index": abs_start},
                "text": replacement,
            }
        })

    await retry_transient(
        lambda: docs_service.documents()
        .batchUpdate(documentId=file_id, body={"requests": requests})
        .execute()
    )

    return {
        "results": results,
        "committed": True,
        "total_replacements": total,
    }
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gdoc_batch_replace.py -v`
Expected: All 9 tests PASS

**Step 5: Commit**

---

### Task 2: `gdoc_ops.batch_replace()` — revision ID wrapper with tests

**Files:**
- Modify: `src/gsuite_mcp/gdoc_ops.py` (add `batch_replace` function)
- Modify: `tests/test_gdoc_batch_replace.py` (add wrapper tests)

**Step 1: Write failing tests for `gdoc_ops.batch_replace`**

Append to `tests/test_gdoc_batch_replace.py`:

```python
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
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gdoc_batch_replace.py::test_gdoc_ops_batch_replace_includes_revision_ids tests/test_gdoc_batch_replace.py::test_gdoc_ops_batch_replace_dry_run_skips_after_revision tests/test_gdoc_batch_replace.py::test_gdoc_ops_batch_replace_aborted_skips_after_revision -v`
Expected: FAIL with `ImportError: cannot import name 'batch_replace' from 'gsuite_mcp.gdoc_ops'`

**Step 3: Implement `gdoc_ops.batch_replace()`**

Add to the end of `src/gsuite_mcp/gdoc_ops.py`:

```python
async def batch_replace(
    drive_service,
    docs_service,
    file_id: str,
    edits: list[dict],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Batch find/replace with Drive revision ID anchors.

    Wraps docs_ops.batch_replace, adding revision_id_before (always)
    and revision_id_after (only when committed) from Drive revisions.list.
    """
    from gsuite_mcp import docs_ops

    # Fetch latest Drive revision ID before mutation
    rev_resp = await asyncio.to_thread(
        lambda: drive_service.revisions()
        .list(fileId=file_id, fields="revisions(id)", pageSize=1000)
        .execute()
    )
    revisions = rev_resp.get("revisions", [])
    revision_id_before = revisions[-1]["id"] if revisions else None

    result = await docs_ops.batch_replace(
        docs_service, file_id, edits, dry_run=dry_run,
    )

    result["revision_id_before"] = revision_id_before

    if result.get("committed"):
        rev_resp_after = await asyncio.to_thread(
            lambda: drive_service.revisions()
            .list(fileId=file_id, fields="revisions(id)", pageSize=1000)
            .execute()
        )
        revisions_after = rev_resp_after.get("revisions", [])
        result["revision_id_after"] = revisions_after[-1]["id"] if revisions_after else None

    return result
```

Note: Drive `revisions.list` doesn't support `orderBy` — it returns revisions in chronological order. We take the last element. `pageSize=1000` ensures we get the latest even on docs with many revisions.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gdoc_batch_replace.py -v`
Expected: All 12 tests PASS

**Step 5: Commit**

---

### Task 3: `server.gdoc_batch_replace` — MCP tool with guards

**Files:**
- Modify: `src/gsuite_mcp/server.py` (add tool function)
- Modify: `tests/test_gdoc_batch_replace.py` (add server-level tests)

**Step 1: Write failing tests for the server tool**

Append to `tests/test_gdoc_batch_replace.py`:

```python
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
    # Second files().get for modifiedTime after commit
    drive.files().get.return_value.execute.return_value = {
        "name": "Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-06-02T12:00:00Z",
    }

    from gsuite_mcp.server import gdoc_batch_replace
    result = await gdoc_batch_replace(
        file_id="f1",
        edits=[{"find_text": "Hello", "replace_text": "Hi"}],
    )
    assert result["file_id"] == "f1"
    assert result["committed"] is True
    assert "modified_time" in result
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gdoc_batch_replace.py -k "test_tool_" -v`
Expected: FAIL with `ImportError: cannot import name 'gdoc_batch_replace' from 'gsuite_mcp.server'`

**Step 3: Implement `server.gdoc_batch_replace`**

Add to `src/gsuite_mcp/server.py` after the `gdoc_suggest_edit` tool (around line 691):

```python
@mcp.tool()
async def gdoc_batch_replace(
    file_id: str,
    edits: list[dict[str, Any]],
    dry_run: bool = False,
    allow_review_docs: bool = False,
) -> dict[str, Any]:
    """Batch find/replace in a live Google Doc, in place.

    Accepts an array of find/replace pairs applied atomically in one
    batchUpdate. Supports cross-paragraph matches. Preserves file ID.

    Each edit: {find_text: str, replace_text: str, expected_count?: int}.
    If any pair's expected_count doesn't match, the entire batch aborts.

    dry_run=True returns per-pair match counts without writing.
    allow_review_docs=False (default) blocks edits to hand-review docs
    listed in the GDOC_REVIEW_DOC_IDS env var.

    Returns revision_id_before/after (Drive revision IDs) for rollback.
    Refuses trashed files with error: TRASHED_FILE."""
    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(fileId=file_id, fields="name,mimeType,modifiedTime,trashed,trashedTime")
        .execute()
    )
    if meta.get("trashed"):
        return _trashed_error(file_id, meta)
    if meta.get("mimeType") != GOOGLE_DOC_MIME:
        return {
            "error": "NOT_A_GOOGLE_DOC",
            "retryable": False,
            "message": (
                f"gdoc_batch_replace only works on Google Docs. This file is "
                f"{meta.get('mimeType')}."
            ),
        }

    # Denylist guard
    review_ids_raw = os.environ.get("GDOC_REVIEW_DOC_IDS", "")
    review_ids = {rid.strip() for rid in review_ids_raw.split(",") if rid.strip()}
    if file_id in review_ids and not allow_review_docs:
        return {
            "error": "REVIEW_DOC_BLOCKED",
            "retryable": False,
            "file_id": file_id,
            "file_name": meta.get("name", ""),
            "message": (
                "This document is in the hand-review denylist "
                "(GDOC_REVIEW_DOC_IDS). Use gdoc_suggest_edit for review "
                "docs, or pass allow_review_docs=True to override."
            ),
        }

    # Validate edits
    if not edits:
        return {
            "error": "INVALID_INPUT",
            "retryable": False,
            "message": "edits array must not be empty.",
        }
    for i, edit in enumerate(edits):
        if "find_text" not in edit or "replace_text" not in edit:
            return {
                "error": "INVALID_INPUT",
                "retryable": False,
                "message": (
                    f"Edit at index {i} missing required field(s). "
                    f"Each edit must have 'find_text' and 'replace_text'."
                ),
            }

    docs = auth.get_docs_service()
    try:
        result = await gdoc_ops.batch_replace(
            drive, docs, file_id, edits, dry_run=dry_run,
        )
        result["file_id"] = file_id
        if result.get("committed"):
            meta2 = await asyncio.to_thread(
                lambda: drive.files()
                .get(fileId=file_id, fields="modifiedTime")
                .execute()
            )
            result["modified_time"] = meta2.get("modifiedTime", "")
        return result
    except HttpError as exc:
        status = exc.resp.status if exc.resp else 0
        return {
            "error": "GOOGLE_API_ERROR",
            "retryable": status in TRANSIENT_CODES,
            "http_status": status,
            "message": (
                f"Google Docs API error (HTTP {status}) after retries: {exc}"
            ),
        }
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gdoc_batch_replace.py -v`
Expected: All 19 tests PASS

**Step 5: Run the full test suite**

Run: `uv run pytest -q`
Expected: All existing tests still pass, plus the 19 new ones.

**Step 6: Commit**

---

### Task 4: Update CLAUDE.md and project docs

**Files:**
- Modify: `CLAUDE.md`

**Step 1: Update CLAUDE.md**

In the **Tools** list, add after tool 17:

```
18. `gdoc_batch_replace` — batch find/replace in a live Google Doc (in-place, atomic, cross-paragraph, count verification, dry-run, review-doc denylist)
```

In the **Environment Variables** optional section, add:

```
- `GDOC_REVIEW_DOC_IDS` — comma-separated file IDs for hand-review docs that `gdoc_batch_replace` will refuse without `allow_review_docs=True`
```

In the **Key Constraints** section, add:

```
- `gdoc_batch_replace` uses client-side matching with `deleteContentRange`+`insertText` (not `replaceAllText`), so cross-paragraph find/replace works. Always case-sensitive.
```

Update session tracking.

**Step 2: Commit**

**Step 3: Run lint**

Run: `uv run ruff check .`
Expected: Clean

**Step 4: Run full test suite one final time**

Run: `uv run pytest -q`
Expected: All tests pass

---

Plan complete and saved to `docs/plans/2026-06-02-gdoc-batch-replace.md`. Two execution options:

**1. Subagent-Driven (this session)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Parallel Session (separate)** - Open new session with executing-plans, batch execution with checkpoints

Which approach?