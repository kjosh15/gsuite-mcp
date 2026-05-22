# Replace Text Enhancements Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add expected_count safety, context-anchored replacement, and list-path-based paragraph reading to the Google Docs tools.

**Architecture:** All three features build on the existing client-side document read pattern already used by `replace_regex`. Feature 1 adds a pre-check gate. Feature 2 adds post-match filtering. Feature 3 adds a new tree-building layer over the existing paragraph data model.

**Tech Stack:** Python 3.12+, Google Docs API v1, FastMCP, pytest + unittest.mock

---

### Task 1: `expected_count` — extract `_flatten_doc` helper from `replace_regex`

The flatten-doc-text-with-index-map logic in `replace_regex` will be reused by features 1 and 2. Extract it first.

**Files:**
- Modify: `src/gsuite_mcp/docs_ops.py:349-409` (replace_regex function)
- Test: `tests/test_replace_text.py` (existing tests stay green)

**Step 1: Write the failing test**

```python
# tests/test_docs_ops_helpers.py
from gsuite_mcp.docs_ops import _flatten_doc_text


def test_flatten_doc_text_single_paragraph():
    doc = {
        "body": {
            "content": [
                {
                    "startIndex": 1, "endIndex": 12,
                    "paragraph": {
                        "elements": [
                            {"startIndex": 1, "endIndex": 12,
                             "textRun": {"content": "hello world\n"}}
                        ]
                    },
                }
            ]
        }
    }
    flat, index_map = _flatten_doc_text(doc)
    assert flat == "hello world\n"
    assert len(index_map) == len(flat)
    assert index_map[0] == 1  # first char maps to doc index 1
    assert index_map[-1] == 12  # last char (\n) maps to doc index 12


def test_flatten_doc_text_multiple_paragraphs():
    doc = {
        "body": {
            "content": [
                {
                    "startIndex": 1, "endIndex": 7,
                    "paragraph": {
                        "elements": [
                            {"startIndex": 1, "endIndex": 7,
                             "textRun": {"content": "first\n"}}
                        ]
                    },
                },
                {
                    "startIndex": 7, "endIndex": 14,
                    "paragraph": {
                        "elements": [
                            {"startIndex": 7, "endIndex": 14,
                             "textRun": {"content": "second\n"}}
                        ]
                    },
                },
            ]
        }
    }
    flat, index_map = _flatten_doc_text(doc)
    assert flat == "first\nsecond\n"
    assert index_map[0] == 1
    assert index_map[6] == 7  # 's' of "second"


def test_flatten_doc_text_empty_doc():
    doc = {"body": {"content": []}}
    flat, index_map = _flatten_doc_text(doc)
    assert flat == ""
    assert index_map == []
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_docs_ops_helpers.py -v`
Expected: FAIL with ImportError — `_flatten_doc_text` doesn't exist yet

**Step 3: Write minimal implementation**

In `src/gsuite_mcp/docs_ops.py`, add after the `_para_text` function (around line 34):

```python
def _flatten_doc_text(doc: dict) -> tuple[str, list[int]]:
    """Flatten all textRun content into a single string with an index map.

    Returns (flat_text, index_map) where index_map[i] is the absolute
    document index of flat_text[i].
    """
    flat_parts: list[str] = []
    index_map: list[int] = []
    for block in doc.get("body", {}).get("content", []):
        para = block.get("paragraph")
        if not para:
            continue
        for elem in para.get("elements", []):
            tr = elem.get("textRun")
            if not tr:
                continue
            start_idx = elem["startIndex"]
            content = tr.get("content", "")
            for offset, ch in enumerate(content):
                flat_parts.append(ch)
                index_map.append(start_idx + offset)
    return "".join(flat_parts), index_map
```

Then refactor `replace_regex` to use it — replace lines 362-381 with:

```python
    flat, index_map = _flatten_doc_text(doc)

    matches = list(regex.finditer(flat))
```

**Step 4: Run tests to verify everything passes**

Run: `uv run pytest tests/test_docs_ops_helpers.py tests/test_replace_text.py -v`
Expected: ALL PASS

**Step 5: Commit**

Autocommit hook handles this.

---

### Task 2: `expected_count` — add `count_occurrences` to `docs_ops.py`

**Files:**
- Modify: `src/gsuite_mcp/docs_ops.py`
- Test: `tests/test_docs_ops_helpers.py`

**Step 1: Write the failing test**

```python
# append to tests/test_docs_ops_helpers.py
import pytest
from gsuite_mcp.docs_ops import _count_occurrences


def test_count_occurrences_exact_case_sensitive():
    flat = "foo bar foo baz Foo"
    assert _count_occurrences(flat, "foo", match_case=True, regex=False) == 2


def test_count_occurrences_exact_case_insensitive():
    flat = "foo bar foo baz Foo"
    assert _count_occurrences(flat, "foo", match_case=False, regex=False) == 3


def test_count_occurrences_regex():
    flat = "version v1.2 and v3.4 text"
    assert _count_occurrences(flat, r"v\d+\.\d+", match_case=True, regex=True) == 2


def test_count_occurrences_no_match():
    flat = "hello world"
    assert _count_occurrences(flat, "xyz", match_case=True, regex=False) == 0
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_docs_ops_helpers.py::test_count_occurrences_exact_case_sensitive -v`
Expected: FAIL with ImportError

**Step 3: Write minimal implementation**

```python
def _count_occurrences(
    flat_text: str, find: str, *, match_case: bool, regex: bool
) -> int:
    """Count occurrences of *find* in *flat_text*."""
    if regex:
        flags = 0 if match_case else re.IGNORECASE
        return len(re.findall(find, flat_text, flags))
    if not match_case:
        return flat_text.casefold().count(find.casefold())
    return flat_text.count(find)
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_docs_ops_helpers.py -v`
Expected: ALL PASS

**Step 5: Commit**

---

### Task 3: `expected_count` — wire into `replace_text` tool

**Files:**
- Modify: `src/gsuite_mcp/docs_ops.py` (`replace_all_text` and `replace_regex`)
- Modify: `src/gsuite_mcp/server.py:185-269` (`replace_text` tool)
- Test: `tests/test_replace_text.py`

**Step 1: Write the failing tests**

```python
# append to tests/test_replace_text.py

@pytest.mark.asyncio
async def test_replace_text_expected_count_match_proceeds(mock_services):
    """When expected_count matches actual, replacement proceeds normally."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]
    drive.files().get.return_value.execute.return_value = {
        "name": "doc", "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }
    # documents.get for pre-check
    docs.documents().get.return_value.execute.return_value = {
        "body": {
            "content": [
                {
                    "startIndex": 1, "endIndex": 20,
                    "paragraph": {
                        "elements": [
                            {"startIndex": 1, "endIndex": 20,
                             "textRun": {"content": "foo bar foo baz\n"}}
                        ]
                    },
                }
            ]
        }
    }
    docs.documents().batchUpdate.return_value.execute.return_value = {
        "replies": [{"replaceAllText": {"occurrencesChanged": 2}}]
    }

    from gsuite_mcp.server import replace_text
    result = await replace_text(
        file_id="d1", find="foo", replace="qux", expected_count=2
    )
    assert result["replacements_made"] == 2
    assert "error" not in result


@pytest.mark.asyncio
async def test_replace_text_expected_count_mismatch_blocks(mock_services):
    """When expected_count doesn't match, no mutation happens."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]
    drive.files().get.return_value.execute.return_value = {
        "name": "doc", "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }
    docs.documents().get.return_value.execute.return_value = {
        "body": {
            "content": [
                {
                    "startIndex": 1, "endIndex": 20,
                    "paragraph": {
                        "elements": [
                            {"startIndex": 1, "endIndex": 20,
                             "textRun": {"content": "foo bar foo baz\n"}}
                        ]
                    },
                }
            ]
        }
    }

    from gsuite_mcp.server import replace_text
    result = await replace_text(
        file_id="d1", find="foo", replace="qux", expected_count=1
    )
    assert result["error"] == "COUNT_MISMATCH"
    assert result["expected_count"] == 1
    assert result["actual_count"] == 2
    # batchUpdate should NOT have been called
    docs.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_replace_text_expected_count_regex_mismatch(mock_services):
    """expected_count works with regex mode too."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]
    drive.files().get.return_value.execute.return_value = {
        "name": "doc", "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }
    docs.documents().get.return_value.execute.return_value = {
        "body": {
            "content": [
                {
                    "startIndex": 1, "endIndex": 30,
                    "paragraph": {
                        "elements": [
                            {"startIndex": 1, "endIndex": 30,
                             "textRun": {"content": "v1.2 and v3.4 and v5.6\n"}}
                        ]
                    },
                }
            ]
        }
    }

    from gsuite_mcp.server import replace_text
    result = await replace_text(
        file_id="d1", find=r"v\d+\.\d+", replace="vX",
        regex=True, expected_count=2
    )
    assert result["error"] == "COUNT_MISMATCH"
    assert result["actual_count"] == 3
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_replace_text.py::test_replace_text_expected_count_match_proceeds tests/test_replace_text.py::test_replace_text_expected_count_mismatch_blocks tests/test_replace_text.py::test_replace_text_expected_count_regex_mismatch -v`
Expected: FAIL — `replace_text` doesn't accept `expected_count`

**Step 3: Implement**

In `src/gsuite_mcp/docs_ops.py`, modify `replace_all_text`:

```python
async def replace_all_text(
    docs_service, file_id: str, find: str, replace: str, match_case: bool,
    expected_count: int | None = None,
) -> int | dict[str, Any]:
    """Exact-match replace across a Google Doc. Returns occurrence count.

    When expected_count is set, fetches the document first and counts
    occurrences client-side. Returns a COUNT_MISMATCH error dict if
    the count doesn't match — no mutation occurs.
    """
    if expected_count is not None:
        doc = await asyncio.to_thread(
            lambda: docs_service.documents()
            .get(documentId=file_id)
            .execute()
        )
        flat, _ = _flatten_doc_text(doc)
        actual = _count_occurrences(flat, find, match_case=match_case, regex=False)
        if actual != expected_count:
            return {
                "error": "COUNT_MISMATCH",
                "retryable": False,
                "expected_count": expected_count,
                "actual_count": actual,
                "message": f"Expected {expected_count} occurrence(s) but found {actual}. No changes made.",
            }

    requests = [
        {
            "replaceAllText": {
                "containsText": {"text": find, "matchCase": match_case},
                "replaceText": replace,
            }
        }
    ]
    resp = await retry_transient(
        lambda: docs_service.documents()
        .batchUpdate(documentId=file_id, body={"requests": requests})
        .execute()
    )
    reply = resp.get("replies", [{}])[0]
    return reply.get("replaceAllText", {}).get("occurrencesChanged", 0)
```

Modify `replace_regex` to accept `expected_count`:

```python
async def replace_regex(
    docs_service, file_id: str, pattern: str, replacement: str, match_case: bool,
    expected_count: int | None = None,
) -> int | dict[str, Any]:
```

After computing `matches = list(regex.finditer(flat))`, add:

```python
    if expected_count is not None and len(matches) != expected_count:
        return {
            "error": "COUNT_MISMATCH",
            "retryable": False,
            "expected_count": expected_count,
            "actual_count": len(matches),
            "message": f"Expected {expected_count} occurrence(s) but found {len(matches)}. No changes made.",
        }
```

In `src/gsuite_mcp/server.py`, modify the `replace_text` tool:

```python
@mcp.tool()
async def replace_text(
    file_id: str,
    find: str,
    replace: str,
    match_case: bool = True,
    regex: bool = False,
    expected_count: Optional[int] = None,
) -> dict[str, Any]:
```

Update the docstring to document `expected_count`.

In the exact-mode branch, pass `expected_count`:

```python
        result = await docs_ops.replace_all_text(
            docs, file_id, find, replace, match_case, expected_count=expected_count
        )
        if isinstance(result, dict) and "error" in result:
            return result
        count = result
```

In the regex branch, pass `expected_count`:

```python
            result = await docs_ops.replace_regex(
                docs, file_id, find, replace, match_case, expected_count=expected_count
            )
            if isinstance(result, dict) and "error" in result:
                return result
            count = result
```

**Step 4: Run all tests**

Run: `uv run pytest tests/test_replace_text.py -v`
Expected: ALL PASS

**Step 5: Commit**

---

### Task 4: `preceded_by`/`followed_by` — add `replace_in_context` to `docs_ops.py`

**Files:**
- Modify: `src/gsuite_mcp/docs_ops.py`
- Create: `tests/test_replace_in_context.py`

**Step 1: Write the failing tests**

```python
# tests/test_replace_in_context.py
from unittest.mock import patch, MagicMock
import pytest

SAMPLE_DOC = {
    "body": {
        "content": [
            {
                "startIndex": 1, "endIndex": 50,
                "paragraph": {
                    "elements": [
                        {"startIndex": 1, "endIndex": 50,
                         "textRun": {"content": "In section A: replace foo with bar here.\n"}}
                    ]
                },
            },
            {
                "startIndex": 50, "endIndex": 100,
                "paragraph": {
                    "elements": [
                        {"startIndex": 50, "endIndex": 100,
                         "textRun": {"content": "In section B: keep foo unchanged here.\n"}}
                    ]
                },
            },
        ]
    }
}


@pytest.fixture
def mock_docs():
    docs = MagicMock()
    docs.documents().get.return_value.execute.return_value = SAMPLE_DOC
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}
    return docs


@pytest.mark.asyncio
async def test_replace_in_context_preceded_by(mock_docs):
    from gsuite_mcp.docs_ops import replace_in_context
    count = await replace_in_context(
        mock_docs, "f1", "foo", "qux", match_case=True,
        preceded_by="section A",
    )
    assert count == 1
    # Only one delete+insert pair
    req = mock_docs.documents().batchUpdate.call_args.kwargs["body"]["requests"]
    assert len(req) == 2


@pytest.mark.asyncio
async def test_replace_in_context_followed_by(mock_docs):
    from gsuite_mcp.docs_ops import replace_in_context
    count = await replace_in_context(
        mock_docs, "f1", "foo", "qux", match_case=True,
        followed_by="unchanged",
    )
    assert count == 1


@pytest.mark.asyncio
async def test_replace_in_context_both_anchors(mock_docs):
    from gsuite_mcp.docs_ops import replace_in_context
    count = await replace_in_context(
        mock_docs, "f1", "foo", "qux", match_case=True,
        preceded_by="section A", followed_by="bar",
    )
    assert count == 1


@pytest.mark.asyncio
async def test_replace_in_context_no_context_match(mock_docs):
    from gsuite_mcp.docs_ops import replace_in_context
    count = await replace_in_context(
        mock_docs, "f1", "foo", "qux", match_case=True,
        preceded_by="section C",
    )
    assert count == 0
    mock_docs.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_replace_in_context_with_expected_count_mismatch(mock_docs):
    from gsuite_mcp.docs_ops import replace_in_context
    result = await replace_in_context(
        mock_docs, "f1", "foo", "qux", match_case=True,
        preceded_by="section A", expected_count=2,
    )
    assert isinstance(result, dict)
    assert result["error"] == "COUNT_MISMATCH"
    assert result["actual_count"] == 1
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_replace_in_context.py -v`
Expected: FAIL — `replace_in_context` doesn't exist

**Step 3: Implement**

In `src/gsuite_mcp/docs_ops.py`:

```python
CONTEXT_WINDOW = 200


async def replace_in_context(
    docs_service,
    file_id: str,
    find: str,
    replace: str,
    match_case: bool,
    preceded_by: str | None = None,
    followed_by: str | None = None,
    regex: bool = False,
    expected_count: int | None = None,
) -> int | dict[str, Any]:
    """Replace text with context-anchor filtering.

    Finds all occurrences of *find*, then keeps only those where
    *preceded_by* appears within 200 chars before the match and/or
    *followed_by* appears within 200 chars after. Executes filtered
    replacements as a single batchUpdate.
    """
    doc = await asyncio.to_thread(
        lambda: docs_service.documents()
        .get(documentId=file_id)
        .execute()
    )
    flat, index_map = _flatten_doc_text(doc)

    if regex:
        flags = 0 if match_case else re.IGNORECASE
        pattern = re.compile(find, flags)
        raw_matches = list(pattern.finditer(flat))
    else:
        raw_matches = _find_all_substring(flat, find, match_case)

    # Filter by context anchors
    filtered = []
    for m_start, m_end in _normalize_matches(raw_matches, regex):
        if preceded_by is not None:
            window_start = max(0, m_start - CONTEXT_WINDOW)
            before_text = flat[window_start:m_start]
            if match_case:
                if preceded_by not in before_text:
                    continue
            else:
                if preceded_by.casefold() not in before_text.casefold():
                    continue
        if followed_by is not None:
            window_end = min(len(flat), m_end + CONTEXT_WINDOW)
            after_text = flat[m_end:window_end]
            if match_case:
                if followed_by not in after_text:
                    continue
            else:
                if followed_by.casefold() not in after_text.casefold():
                    continue
        filtered.append((m_start, m_end))

    if expected_count is not None and len(filtered) != expected_count:
        return {
            "error": "COUNT_MISMATCH",
            "retryable": False,
            "expected_count": expected_count,
            "actual_count": len(filtered),
            "message": f"Expected {expected_count} occurrence(s) but found {len(filtered)} after context filtering. No changes made.",
        }

    if not filtered:
        return 0

    # Build requests in REVERSE order
    requests: list[dict] = []
    for m_start, m_end in reversed(filtered):
        abs_start = index_map[m_start]
        abs_end = index_map[m_end - 1] + 1
        if regex:
            replacement_text = re.compile(find).search(flat[m_start:m_end]).expand(replace)
        else:
            replacement_text = replace
        requests.append({
            "deleteContentRange": {
                "range": {"startIndex": abs_start, "endIndex": abs_end}
            }
        })
        requests.append({
            "insertText": {
                "location": {"index": abs_start},
                "text": replacement_text,
            }
        })

    await retry_transient(
        lambda: docs_service.documents()
        .batchUpdate(documentId=file_id, body={"requests": requests})
        .execute()
    )
    return len(filtered)


def _find_all_substring(
    text: str, needle: str, match_case: bool
) -> list[tuple[int, int]]:
    """Find all (start, end) positions of needle in text."""
    if not match_case:
        search_text = text.casefold()
        search_needle = needle.casefold()
    else:
        search_text = text
        search_needle = needle
    results = []
    start = 0
    while True:
        idx = search_text.find(search_needle, start)
        if idx == -1:
            break
        results.append((idx, idx + len(needle)))
        start = idx + 1
    return results


def _normalize_matches(
    matches: list, regex: bool
) -> list[tuple[int, int]]:
    """Normalize regex Match objects or (start, end) tuples to (start, end)."""
    if regex:
        return [(m.start(), m.end()) for m in matches]
    return matches
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_replace_in_context.py -v`
Expected: ALL PASS

**Step 5: Commit**

---

### Task 5: Wire `preceded_by`/`followed_by` into the `replace_text` tool

**Files:**
- Modify: `src/gsuite_mcp/server.py:185-269`
- Test: `tests/test_replace_text.py`

**Step 1: Write the failing test**

```python
# append to tests/test_replace_text.py

@pytest.mark.asyncio
async def test_replace_text_with_preceded_by(mock_services):
    drive = mock_services["drive"]
    docs = mock_services["docs"]
    drive.files().get.return_value.execute.return_value = {
        "name": "doc", "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }
    docs.documents().get.return_value.execute.return_value = {
        "body": {
            "content": [
                {
                    "startIndex": 1, "endIndex": 50,
                    "paragraph": {
                        "elements": [
                            {"startIndex": 1, "endIndex": 50,
                             "textRun": {"content": "In section A: foo is here.\n"}}
                        ]
                    },
                },
                {
                    "startIndex": 50, "endIndex": 100,
                    "paragraph": {
                        "elements": [
                            {"startIndex": 50, "endIndex": 100,
                             "textRun": {"content": "In section B: foo is here too.\n"}}
                        ]
                    },
                },
            ]
        }
    }
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    from gsuite_mcp.server import replace_text
    result = await replace_text(
        file_id="d1", find="foo", replace="bar", preceded_by="section A"
    )
    assert result["replacements_made"] == 1
    assert result.get("context_filtered") is True
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_replace_text.py::test_replace_text_with_preceded_by -v`
Expected: FAIL — `replace_text` doesn't accept `preceded_by`

**Step 3: Implement**

In `src/gsuite_mcp/server.py`, update the tool signature:

```python
@mcp.tool()
async def replace_text(
    file_id: str,
    find: str,
    replace: str,
    match_case: bool = True,
    regex: bool = False,
    expected_count: Optional[int] = None,
    preceded_by: Optional[str] = None,
    followed_by: Optional[str] = None,
) -> dict[str, Any]:
```

Update the docstring to document the new params.

In the body, add a branch before the existing regex/exact split — when context params are provided, route to `replace_in_context`:

```python
    try:
        # Context-anchored mode: use client-side approach
        if preceded_by is not None or followed_by is not None:
            result = await docs_ops.replace_in_context(
                docs, file_id, find, replace, match_case,
                preceded_by=preceded_by, followed_by=followed_by,
                regex=regex, expected_count=expected_count,
            )
            if isinstance(result, dict) and "error" in result:
                return result
            count = result
            meta2 = await asyncio.to_thread(
                lambda: drive.files()
                .get(fileId=file_id, fields="modifiedTime")
                .execute()
            )
            return {
                "file_id": file_id,
                "replacements_made": count,
                "regex_mode": regex,
                "context_filtered": True,
                "modified_time": meta2.get("modifiedTime", ""),
            }

        if regex:
            # ... existing regex branch (with expected_count)
```

**Step 4: Run all tests**

Run: `uv run pytest tests/test_replace_text.py tests/test_replace_in_context.py -v`
Expected: ALL PASS

**Step 5: Commit**

---

### Task 6: `read_paragraph_at_path` — tree builder in `docs_ops.py`

**Files:**
- Modify: `src/gsuite_mcp/docs_ops.py`
- Create: `tests/test_read_paragraph_at_path.py`

**Step 1: Write the failing tests**

```python
# tests/test_read_paragraph_at_path.py
import pytest
from gsuite_mcp.docs_ops import _build_doc_tree, _resolve_path


def _make_heading(idx, start, end, text, style="HEADING_1"):
    return {
        "startIndex": start, "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": style},
            "elements": [
                {"startIndex": start, "endIndex": end,
                 "textRun": {"content": text + "\n"}}
            ],
        },
    }


def _make_bullet(idx, start, end, text, nesting_level=0):
    return {
        "startIndex": start, "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "bullet": {"nestingLevel": nesting_level},
            "elements": [
                {"startIndex": start, "endIndex": end,
                 "textRun": {"content": text + "\n"}}
            ],
        },
    }


SAMPLE_CONTENT = [
    {"startIndex": 0, "endIndex": 1},  # structural element (no paragraph)
    _make_heading(1, 1, 7, "TASKS", "HEADING_1"),
    _make_bullet(2, 7, 15, "Career", 0),
    _make_bullet(3, 15, 50, "Careers that allow us to live in Iceland", 1),
    _make_bullet(4, 50, 70, "Teaching English", 2),
    _make_bullet(5, 70, 95, "Software engineering", 2),
    _make_bullet(6, 95, 110, "Finance", 1),
    _make_heading(7, 110, 120, "NOTES", "HEADING_1"),
    _make_bullet(8, 120, 135, "Random note", 0),
]


def test_build_doc_tree():
    tree = _build_doc_tree(SAMPLE_CONTENT)
    # Top level should have 2 heading nodes: TASKS, NOTES
    assert len(tree) == 2
    assert tree[0]["text"] == "TASKS"
    assert tree[1]["text"] == "NOTES"


def test_resolve_path_heading():
    tree = _build_doc_tree(SAMPLE_CONTENT)
    node = _resolve_path(tree, ["TASKS"])
    assert node is not None
    assert node["text"] == "TASKS"


def test_resolve_path_nested_bullet():
    tree = _build_doc_tree(SAMPLE_CONTENT)
    node = _resolve_path(tree, ["TASKS", "Career", "Careers that allow"])
    assert node is not None
    assert "Careers that allow" in node["text"]


def test_resolve_path_positional():
    tree = _build_doc_tree(SAMPLE_CONTENT)
    node = _resolve_path(tree, ["TASKS", "Career", "#2"])
    assert node is not None
    assert node["text"].startswith("Finance")


def test_resolve_path_not_found():
    tree = _build_doc_tree(SAMPLE_CONTENT)
    node = _resolve_path(tree, ["TASKS", "Nonexistent"])
    assert node is None


def test_resolve_path_include_children():
    tree = _build_doc_tree(SAMPLE_CONTENT)
    node = _resolve_path(tree, ["TASKS", "Career", "Careers that allow"])
    assert node is not None
    assert len(node.get("children", [])) == 2
    assert node["children"][0]["text"] == "Teaching English"
    assert node["children"][1]["text"] == "Software engineering"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_read_paragraph_at_path.py -v`
Expected: FAIL — functions don't exist

**Step 3: Implement**

In `src/gsuite_mcp/docs_ops.py`:

```python
# ---------------------------------------------------------------------------
# Document tree builder for path-based navigation
# ---------------------------------------------------------------------------


def _build_doc_tree(content: list[dict]) -> list[dict]:
    """Build a tree from document content using headings and bullet nesting.

    Returns a list of top-level nodes. Each node has:
    - text: paragraph text (stripped)
    - start_index, end_index: document indices
    - paragraph_index: index in content list
    - nesting_level: 0 for headings, bullet nestingLevel for list items
    - heading_level: named style if a heading, else None
    - children: list of child nodes
    """
    nodes: list[dict] = []

    def _make_node(block: dict, block_idx: int) -> dict:
        para = block["paragraph"]
        text = _para_text(para).strip()
        style = para.get("paragraphStyle", {}).get("namedStyleType", "")
        bullet = para.get("bullet")
        nesting = bullet["nestingLevel"] if bullet else 0
        return {
            "text": text,
            "start_index": block["startIndex"],
            "end_index": block["endIndex"],
            "paragraph_index": block_idx,
            "nesting_level": nesting,
            "heading_level": style if style in _HEADING_RANKS else None,
            "children": [],
        }

    # First pass: collect all paragraph nodes
    all_nodes: list[dict] = []
    for idx, block in enumerate(content):
        if not block.get("paragraph"):
            continue
        all_nodes.append(_make_node(block, idx))

    if not all_nodes:
        return []

    # Build tree: headings are top-level, bullets nest by nestingLevel
    # Stack tracks the current path: [(node, effective_depth)]
    # Headings get depth -1 (always top-level)
    root: list[dict] = []
    stack: list[tuple[dict, int]] = []  # (node, depth)

    for node in all_nodes:
        if node["heading_level"] is not None:
            # Heading: always top-level
            root.append(node)
            stack = [(node, -1)]
        else:
            depth = node["nesting_level"]
            # Pop stack until we find a parent at a lower depth
            while stack and stack[-1][1] >= depth:
                stack.pop()
            if stack:
                stack[-1][0]["children"].append(node)
            else:
                root.append(node)
            stack.append((node, depth))

    return root


def _resolve_path(
    tree: list[dict], segments: list[str]
) -> dict | None:
    """Walk the tree by path segments, returning the matched node or None.

    Each segment matches by:
    1. Case-insensitive text prefix (first match wins)
    2. Positional #N (1-based) if segment starts with '#'
    """
    current_children = tree
    node = None

    for segment in segments:
        segment = segment.strip()
        found = None

        if segment.startswith("#") and segment[1:].isdigit():
            # Positional index (1-based)
            pos = int(segment[1:])
            if 1 <= pos <= len(current_children):
                found = current_children[pos - 1]
        else:
            # Text prefix match (case-insensitive)
            needle = segment.casefold()
            for child in current_children:
                if child["text"].casefold().startswith(needle):
                    found = child
                    break

        if found is None:
            return None
        node = found
        current_children = found.get("children", [])

    return node
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_read_paragraph_at_path.py -v`
Expected: ALL PASS

**Step 5: Commit**

---

### Task 7: `read_paragraph_at_path` — wire as MCP tool

**Files:**
- Modify: `src/gsuite_mcp/docs_ops.py` (add async `read_at_path` function)
- Modify: `src/gsuite_mcp/server.py` (add tool)
- Test: `tests/test_read_paragraph_at_path.py` (add integration-level tests)

**Step 1: Write the failing tests**

```python
# append to tests/test_read_paragraph_at_path.py
from unittest.mock import patch, MagicMock


@pytest.fixture
def mock_services():
    with patch("gsuite_mcp.auth.get_drive_service") as mock_drive, \
         patch("gsuite_mcp.auth.get_docs_service") as mock_docs:
        drive = MagicMock()
        docs = MagicMock()
        mock_drive.return_value = drive
        mock_docs.return_value = docs
        drive.files().get.return_value.execute.return_value = {
            "name": "doc", "mimeType": "application/vnd.google-apps.document",
        }
        docs.documents().get.return_value.execute.return_value = {
            "body": {"content": SAMPLE_CONTENT}
        }
        yield {"drive": drive, "docs": docs}


@pytest.mark.asyncio
async def test_read_paragraph_at_path_tool(mock_services):
    from gsuite_mcp.server import read_paragraph_at_path
    result = await read_paragraph_at_path(
        file_id="d1", path="TASKS / Career / Careers that allow"
    )
    assert "Careers that allow" in result["text"]
    assert result["nesting_level"] == 1
    assert "children" not in result


@pytest.mark.asyncio
async def test_read_paragraph_at_path_with_children(mock_services):
    from gsuite_mcp.server import read_paragraph_at_path
    result = await read_paragraph_at_path(
        file_id="d1", path="TASKS / Career / Careers that allow",
        include_children=True,
    )
    assert len(result["children"]) == 2


@pytest.mark.asyncio
async def test_read_paragraph_at_path_not_found(mock_services):
    from gsuite_mcp.server import read_paragraph_at_path
    result = await read_paragraph_at_path(
        file_id="d1", path="TASKS / Nonexistent"
    )
    assert result["error"] == "PATH_NOT_FOUND"


@pytest.mark.asyncio
async def test_read_paragraph_at_path_not_a_doc(mock_services):
    mock_services["drive"].files().get.return_value.execute.return_value = {
        "name": "file.txt", "mimeType": "text/plain",
    }
    from gsuite_mcp.server import read_paragraph_at_path
    result = await read_paragraph_at_path(file_id="d1", path="anything")
    assert result["error"] == "NOT_A_GOOGLE_DOC"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_read_paragraph_at_path.py::test_read_paragraph_at_path_tool -v`
Expected: FAIL — tool doesn't exist

**Step 3: Implement**

In `src/gsuite_mcp/docs_ops.py`, add:

```python
async def read_at_path(
    docs_service,
    file_id: str,
    path: str,
    include_children: bool = False,
) -> dict[str, Any]:
    """Read a paragraph by navigating a path through the document tree."""
    doc = await asyncio.to_thread(
        lambda: docs_service.documents()
        .get(documentId=file_id)
        .execute()
    )
    content = doc.get("body", {}).get("content", [])
    tree = _build_doc_tree(content)
    segments = [s.strip() for s in path.split(" / ")]
    node = _resolve_path(tree, segments)

    if node is None:
        return {
            "error": "PATH_NOT_FOUND",
            "retryable": False,
            "message": f"No paragraph found at path: {path}",
        }

    result: dict[str, Any] = {
        "file_id": file_id,
        "path": path,
        "text": node["text"],
        "paragraph_index": node["paragraph_index"],
        "nesting_level": node["nesting_level"],
        "start_index": node["start_index"],
        "end_index": node["end_index"],
    }
    if include_children:
        result["children"] = [
            {
                "text": c["text"],
                "nesting_level": c["nesting_level"],
                "paragraph_index": c["paragraph_index"],
                "start_index": c["start_index"],
                "end_index": c["end_index"],
            }
            for c in node.get("children", [])
        ]
    return result
```

In `src/gsuite_mcp/server.py`, add the tool:

```python
@mcp.tool()
async def read_paragraph_at_path(
    file_id: str,
    path: str,
    include_children: bool = False,
) -> dict[str, Any]:
    """Read a paragraph by navigating a document's heading/list structure via path.

    Path segments are delimited by ' / ' (space-slash-space).
    Each segment matches by case-insensitive text prefix, or by positional
    index '#N' (1-based) among siblings.

    Example: "TASKS / Career / Careers that allow" or "TASKS / Career / #2"

    Args:
        file_id: Google Drive file ID of a native Google Doc.
        path: Path to the target paragraph.
        include_children: If True, include child paragraphs in the response.

    Only works on Google Docs (mimeType application/vnd.google-apps.document).
    """
    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(fileId=file_id, fields="name,mimeType")
        .execute()
    )
    if meta.get("mimeType") != GOOGLE_DOC_MIME:
        return {
            "error": "NOT_A_GOOGLE_DOC",
            "retryable": False,
            "message": (
                f"read_paragraph_at_path only works on Google Docs. "
                f"This file is {meta.get('mimeType')}."
            ),
        }
    docs = auth.get_docs_service()
    return await docs_ops.read_at_path(docs, file_id, path, include_children)
```

**Step 4: Run all tests**

Run: `uv run pytest tests/test_read_paragraph_at_path.py -v`
Expected: ALL PASS

**Step 5: Commit**

---

### Task 8: Update CLAUDE.md and tool count

**Files:**
- Modify: `CLAUDE.md`

**Step 1: Update the project documentation**

Update CLAUDE.md:
- Tool list: add tool 17 `read_paragraph_at_path`
- Update `replace_text` description to mention `expected_count`, `preceded_by`, `followed_by`
- Update test count after running full suite

**Step 2: Run full test suite**

Run: `uv run pytest -q`
Expected: all tests pass, note new count

**Step 3: Run linter**

Run: `uv run ruff check .`
Expected: clean

**Step 4: Commit**

---

### Task 9: Final verification

**Step 1: Run full test suite one more time**

Run: `uv run pytest -v`

**Step 2: Run linter**

Run: `uv run ruff check .`

**Step 3: Verify tool count in MCP server**

Run: `uv run python -c "from gsuite_mcp.server import mcp; print(len(mcp._tool_manager._tools))"`
Expected: 17
