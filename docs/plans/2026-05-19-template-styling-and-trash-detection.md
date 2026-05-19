# Template/Styling Improvements + Trashed File Detection — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix empty-section handling in replace_section, add regex matching to format_document, add post-population styles to gdoc_template_populate, add trashed-file detection and refusal to all operations, and add trash/untrash tools.

**Architecture:** Incremental enhancements to existing ops modules (docs_ops, gdoc_ops, drive_ops) with thin wiring in server.py. No new modules. All changes additive and backward-compatible.

**Tech Stack:** Python 3.12, Google Docs/Drive API v3, FastMCP, pytest + pytest-asyncio

---

## Part A: Template & Styling Improvements

### Task 1: Fix `replace_section` for empty sections

**Files:**
- Modify: `src/gsuite_mcp/docs_ops.py:195-209`
- Test: `tests/test_replace_section.py`

**Step 1: Write the failing tests**

Add to `tests/test_replace_section.py`:

```python
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
    # Should have insertText + updateParagraphStyle, but NO deleteContentRange
    actions = [list(r.keys())[0] for r in requests]
    assert "deleteContentRange" not in actions
    assert "insertText" in actions
    assert "updateParagraphStyle" in actions
    # Insert location should be at heading end_index (10)
    insert_req = [r for r in requests if "insertText" in r][0]
    assert insert_req["insertText"]["location"]["index"] == 10


@pytest.mark.asyncio
async def test_replace_section_empty_last_section_inserts():
    """Last section with no body content inserts after heading."""
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 30, "Some body.\n", "NORMAL_TEXT"),
        (30, 40, "Chapter 2\n", "HEADING_1"),
        # No body after Chapter 2 — end of document
    )
    svc = _mock_docs_service(doc)
    result = await replace_section(svc, "file123", "Chapter 2", "Final content.\n")

    assert "error" not in result
    assert result["characters_deleted"] == 0
    assert result["characters_inserted"] == len("Final content.\n")
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_replace_section.py::test_replace_section_empty_section_inserts_after_heading tests/test_replace_section.py::test_replace_section_empty_last_section_inserts -v`
Expected: FAIL — currently returns `EMPTY_SECTION` error

**Step 3: Implement the fix**

In `src/gsuite_mcp/docs_ops.py`, replace lines 201-209 (the EMPTY_SECTION error block) with:

```python
    # Handle empty section: insert after heading instead of erroring
    empty_section = delete_start >= delete_end and not include_heading

    # Ensure trailing newline
    if not new_content.endswith("\n"):
        new_content += "\n"

    characters_deleted = 0 if empty_section else (delete_end - delete_start)
    characters_inserted = len(new_content)

    insert_index = heading["end_index"] if empty_section else delete_start

    requests: list[dict] = []

    if not empty_section:
        # Clamp delete range to avoid the structural trailing newline
        delete_end_clamped = _clamp_delete_end(delete_end, content)
        requests.append({
            "deleteContentRange": {
                "range": {
                    "startIndex": delete_start,
                    "endIndex": delete_end_clamped,
                }
            }
        })

    requests.append({
        "insertText": {
            "location": {"index": insert_index},
            "text": new_content,
        }
    })
    requests.append({
        "updateParagraphStyle": {
            "range": {
                "startIndex": insert_index,
                "endIndex": insert_index + characters_inserted,
            },
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "fields": "namedStyleType",
        }
    })
```

Also remove the old `characters_deleted`, `characters_inserted`, and `delete_end_clamped` assignments that were below the old error block (lines 211-219), since they're now computed above.

Keep the `include_heading` style restoration block (lines 248-263) unchanged — it only fires when `include_heading=True`, and in the empty_section case `include_heading` is always `False`.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_replace_section.py -v`
Expected: ALL PASS (including old `test_replace_section_empty_section_body` — update it to expect success instead of error)

**Step 5: Update the old empty section test**

Change `test_replace_section_empty_section_body` to expect the new insert behavior:

```python
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
```

**Step 6: Run full test suite**

Run: `uv run pytest -q`
Expected: ALL PASS

---

### Task 2: Add regex match mode to `format_document`

**Files:**
- Modify: `src/gsuite_mcp/docs_ops.py:399-423` (match function), `src/gsuite_mcp/docs_ops.py:487-525` (validation), `src/gsuite_mcp/docs_ops.py:588-591` (usage)
- Test: `tests/test_format_document.py`

**Step 1: Write the failing tests**

Add to `tests/test_format_document.py`:

```python
# -------------------------------------------------------------------
# format_document — regex match mode
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
    # Should match both numbered headings
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
    # Only one match (case-sensitive)
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
    """match_mode takes precedence over substring flag."""
    doc = _make_doc(
        (0, 10, "Hello\n", "NORMAL_TEXT"),
        (10, 20, "hello\n", "NORMAL_TEXT"),
    )
    svc = _mock_docs_service(doc)
    # substring=True would match both, but match_mode="exact" should win
    result = await format_document(svc, "f1", [
        {"action": "delete", "find_text": "Hello", "match_mode": "exact", "substring": True},
    ])
    assert result["results"][0]["status"] == "applied"
    calls = svc.documents().batchUpdate.call_args
    requests = calls.kwargs["body"]["requests"]
    assert len(requests) == 1
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_format_document.py::test_format_regex_match_mode_delete tests/test_format_document.py::test_format_regex_invalid_pattern -v`
Expected: FAIL

**Step 3: Implement regex match mode**

3a. Update `_find_paragraphs_matching` in `src/gsuite_mcp/docs_ops.py`:

```python
def _find_paragraphs_matching(
    content: list[dict],
    find_text: str,
    *,
    substring: bool = False,
    match_mode: str = "exact",
) -> list[tuple[int, dict]]:
    """Return all (block_index, block) pairs whose paragraph text matches.

    match_mode:
      - "exact": strip + casefold equality (default)
      - "substring": needle-in-text after strip + casefold
      - "regex": re.search(find_text, paragraph_text) — case-sensitive
    """
    matches: list[tuple[int, dict]] = []
    # Resolve match_mode from both parameters (match_mode wins)
    effective_mode = match_mode if match_mode != "exact" else ("substring" if substring else "exact")

    if effective_mode == "regex":
        pattern = re.compile(find_text)
        for idx, block in enumerate(content):
            para = block.get("paragraph")
            if not para:
                continue
            text = _para_text(para).strip()
            if pattern.search(text):
                matches.append((idx, block))
    else:
        needle = find_text.strip().casefold()
        for idx, block in enumerate(content):
            para = block.get("paragraph")
            if not para:
                continue
            text = _para_text(para).strip().casefold()
            if effective_mode == "substring":
                if needle in text:
                    matches.append((idx, block))
            else:
                if text == needle:
                    matches.append((idx, block))
    return matches
```

3b. Add regex validation in `format_document` validation section (after the `MISSING_FIND_TEXT` check, around line 514):

```python
        # Validate regex pattern
        if action != "delete_by_index":
            mm = op.get("match_mode", "exact")
            if mm not in ("exact", "substring", "regex"):
                return {
                    "error": "INVALID_MATCH_MODE",
                    "retryable": False,
                    "message": (
                        f"Operation {i}: invalid match_mode '{mm}'. "
                        f"Valid: exact, substring, regex."
                    ),
                }
            if mm == "regex":
                try:
                    re.compile(op.get("find_text", ""))
                except re.error as exc:
                    return {
                        "error": "INVALID_REGEX",
                        "retryable": False,
                        "message": f"Operation {i}: invalid regex '{op.get('find_text', '')}': {exc}",
                    }
```

3c. Update the call site in `format_document` (line 590-591) to pass `match_mode`:

```python
        find_text = op["find_text"]
        use_substring = bool(op.get("substring", False))
        mm = op.get("match_mode", "exact")
        matches = _find_paragraphs_matching(
            content, find_text, substring=use_substring, match_mode=mm,
        )
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_format_document.py -v`
Expected: ALL PASS

**Step 5: Run full test suite**

Run: `uv run pytest -q`
Expected: ALL PASS

---

### Task 3: Add `post_styles` to `gdoc_template_populate`

**Files:**
- Modify: `src/gsuite_mcp/gdoc_ops.py:19-80`
- Modify: `src/gsuite_mcp/server.py:500-533`
- Test: `tests/test_gdoc_template_populate.py`

**Step 1: Write the failing tests**

Add to `tests/test_gdoc_template_populate.py`:

```python
@pytest.mark.asyncio
async def test_template_populate_with_post_styles(mock_services):
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().copy.return_value.execute.return_value = {
        "id": "new_styled",
        "name": "Styled Doc",
        "webViewLink": "https://docs.google.com/document/d/new_styled/edit",
    }
    docs.documents().batchUpdate.return_value.execute.return_value = {
        "replies": [
            {"replaceAllText": {"occurrencesChanged": 1}},
        ]
    }
    # format_document needs documents().get() for doc structure
    docs.documents().get.return_value.execute.return_value = {
        "body": {"content": [
            {
                "startIndex": 0, "endIndex": 20,
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "elements": [{"startIndex": 0, "endIndex": 20,
                                  "textRun": {"content": "Executive Summary\n"}}],
                },
            },
        ]}
    }

    from gsuite_mcp.server import gdoc_template_populate
    result = await gdoc_template_populate(
        template_file_id="tmpl1",
        parent_folder_id="folder1",
        new_title="Styled Doc",
        replacements={"{{NAME}}": "Alice"},
        post_styles=[
            {"action": "set_style", "find_text": "Executive Summary", "style": "HEADING_1"},
        ],
    )

    assert result["file_id"] == "new_styled"
    assert result["replacements_made"] == {"{{NAME}}": 1}
    assert "post_styles_result" in result
    assert result["post_styles_result"]["operations_applied"] == 1


@pytest.mark.asyncio
async def test_template_populate_post_styles_none(mock_services):
    """post_styles=None should not call format_document."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().copy.return_value.execute.return_value = {
        "id": "new123",
        "name": "No Styles",
        "webViewLink": "https://docs.google.com/document/d/new123/edit",
    }
    docs.documents().batchUpdate.return_value.execute.return_value = {
        "replies": [{"replaceAllText": {"occurrencesChanged": 1}}]
    }

    from gsuite_mcp.server import gdoc_template_populate
    result = await gdoc_template_populate(
        template_file_id="tmpl1",
        parent_folder_id="folder1",
        new_title="No Styles",
        replacements={"{{NAME}}": "Bob"},
    )

    assert result["file_id"] == "new123"
    assert "post_styles_result" not in result
    # documents().get() should NOT be called (format_document not invoked)
    docs.documents().get.assert_not_called()
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gdoc_template_populate.py::test_template_populate_with_post_styles tests/test_gdoc_template_populate.py::test_template_populate_post_styles_none -v`
Expected: FAIL — `post_styles` parameter doesn't exist yet

**Step 3: Implement post_styles**

3a. Update `gdoc_ops.template_populate` signature and body:

```python
async def template_populate(
    drive_service,
    docs_service,
    template_file_id: str,
    parent_folder_id: str,
    new_title: str,
    replacements: dict[str, str],
    post_styles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
```

At the end of the function, before the `return`, add:

```python
    result = {
        "file_id": new_file_id,
        "web_view_link": copied.get("webViewLink", ""),
        "replacements_made": replacements_made,
    }

    if post_styles:
        from gsuite_mcp import docs_ops
        style_result = await docs_ops.format_document(
            docs_service, new_file_id, post_styles
        )
        result["post_styles_result"] = style_result

    return result
```

3b. Update `server.py` `gdoc_template_populate` tool to accept and pass `post_styles`:

```python
@mcp.tool()
async def gdoc_template_populate(
    template_file_id: str,
    parent_folder_id: str,
    new_title: str,
    replacements: dict[str, str],
    post_styles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Copy a template file as a native Google Doc and replace placeholders.

    Copies the template using Drive files.copy with automatic .docx-to-Google-Doc
    conversion, places it in the specified parent folder, then issues a single
    documents.batchUpdate with replaceAllText for each placeholder.

    Optionally applies paragraph formatting operations (same schema as
    format_document) after placeholder replacement via post_styles.

    Returns {file_id, web_view_link, replacements_made: {placeholder: count}}.
    """
    try:
        return await gdoc_ops.template_populate(
            drive_service=auth.get_drive_service(),
            docs_service=auth.get_docs_service(),
            template_file_id=template_file_id,
            parent_folder_id=parent_folder_id,
            new_title=new_title,
            replacements=replacements,
            post_styles=post_styles,
        )
    except HttpError as exc:
        status = exc.resp.status if exc.resp else 0
        return {
            "error": "GOOGLE_API_ERROR",
            "retryable": status in TRANSIENT_CODES,
            "http_status": status,
            "message": (
                f"Google API error (HTTP {status}) during template populate: {exc}"
            ),
        }
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gdoc_template_populate.py -v`
Expected: ALL PASS

**Step 5: Run full test suite**

Run: `uv run pytest -q`
Expected: ALL PASS

---

### Task 4: Documentation updates

**Files:**
- Modify: `src/gsuite_mcp/server.py:156-168` (replace_text docstring)
- Modify: `src/gsuite_mcp/server.py:302-337` (format_document docstring)

**Step 1: Update `replace_text` docstring**

In `server.py`, update the `replace_text` docstring (line 164-168):

```python
    """Replace text in a Google Doc. Exact match by default; regex optional.

    Only works on Google Docs (mimeType application/vnd.google-apps.document).
    For real .docx files, use docx_suggest_edit instead.

    Note: Operates on paragraph-level text. Patterns spanning paragraph breaks
    will not match — Google Docs treats paragraph breaks as structural objects,
    not newline characters. To operate across paragraph boundaries, use
    format_document with 'delete' action, or chain multiple replace_text calls.
    """
```

**Step 2: Update `format_document` docstring**

Add `match_mode` documentation to the format_document docstring (line 308-337):

```python
    """Apply paragraph formatting operations to a Google Doc in a single batch.

    Each operation is a dict with an "action" key:

    - set_style: Change paragraph style.
      {"action": "set_style", "find_text": "Introduction", "style": "HEADING_1"}
      Valid styles: NORMAL_TEXT, TITLE, SUBTITLE, HEADING_1..HEADING_6.

    - delete: Delete a paragraph.
      {"action": "delete", "find_text": "Paragraph to remove"}

    - delete_by_index: Delete a paragraph by its content index (from a prior read).
      {"action": "delete_by_index", "paragraph_index": 3}

    - delete_empty_after: Remove blank paragraphs after a matched paragraph.
      {"action": "delete_empty_after", "find_text": "Introduction"}

    Matching rules:
    - find_text matching is exact (strip + case-fold) by default.
    - Add "substring": true on an operation for substring matching.
    - Add "match_mode": "regex" for regex matching (case-sensitive; use (?i) for
      case-insensitive). Invalid patterns return INVALID_REGEX error.
    - "match_mode" takes precedence over "substring" flag. Valid values:
      "exact" (default), "substring", "regex".
    - If a delete or set_style matches multiple paragraphs, it fails with
      a multi_match_error listing all matches (paragraph index + text snippet).
      Pass "match_all": true on the operation to apply to all matches.

    Top-level options:
    - preview: If true, returns what each operation would affect (paragraph
      index + first 80 chars + action) without executing any changes.

    Only works on Google Docs (mimeType application/vnd.google-apps.document).
    """
```

**Step 3: Run full test suite**

Run: `uv run pytest -q`
Expected: ALL PASS (docstring-only changes)

---

## Part B: Trashed File Detection

### Task 5: Add `trashed` field to drive_ops metadata responses

**Files:**
- Modify: `src/gsuite_mcp/drive_ops.py:11-37` (download_file), `src/gsuite_mcp/drive_ops.py:98-120` (search_files), `src/gsuite_mcp/drive_ops.py:123-141` (get_file_metadata)
- Test: `tests/test_drive_ops.py`

**Step 1: Write the failing tests**

Create or add to `tests/test_drive_ops.py`:

```python
"""Tests for drive_ops trashed-file metadata."""

import pytest
from unittest.mock import MagicMock

from gsuite_mcp import drive_ops


def _mock_drive_service(meta_response):
    svc = MagicMock()
    svc.files().get.return_value.execute.return_value = meta_response
    return svc


@pytest.mark.asyncio
async def test_get_file_metadata_trashed_true():
    svc = _mock_drive_service({
        "id": "f1", "name": "Trashed Doc", "mimeType": "application/vnd.google-apps.document",
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
        "id": "f1", "name": "Live Doc", "mimeType": "application/vnd.google-apps.document",
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
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_drive_ops.py -v`
Expected: FAIL — `trashed` key not in responses

**Step 3: Implement trashed field in drive_ops**

3a. `get_file_metadata` — add `trashed,trashedTime` to fields string and map:

```python
async def get_file_metadata(service, file_id: str) -> dict[str, Any]:
    metadata = await asyncio.to_thread(
        lambda: service.files()
        .get(
            fileId=file_id,
            fields="id,name,mimeType,size,modifiedTime,webViewLink,parents,capabilities,trashed,trashedTime",
        )
        .execute()
    )
    result = {
        "file_id": metadata["id"],
        "name": metadata["name"],
        "mime_type": metadata.get("mimeType", ""),
        "size_bytes": int(metadata.get("size", 0)),
        "modified_time": metadata.get("modifiedTime", ""),
        "web_view_link": metadata.get("webViewLink", ""),
        "parents": metadata.get("parents", []),
        "capabilities": metadata.get("capabilities", {}),
        "trashed": metadata.get("trashed", False),
    }
    if result["trashed"]:
        result["trashed_time"] = metadata.get("trashedTime")
    return result
```

3b. `search_files` — add `trashed,trashedTime` to list fields and map:

```python
async def search_files(service, query: str, max_results: int = 10) -> dict[str, Any]:
    response = await asyncio.to_thread(
        lambda: service.files()
        .list(
            q=query,
            pageSize=max_results,
            fields="files(id,name,mimeType,modifiedTime,webViewLink,parents,trashed,trashedTime)",
        )
        .execute()
    )
    files = []
    for f in response.get("files", []):
        entry = {
            "file_id": f["id"],
            "name": f["name"],
            "mime_type": f.get("mimeType", ""),
            "modified_time": f.get("modifiedTime", ""),
            "web_view_link": f.get("webViewLink", ""),
            "parents": f.get("parents", []),
            "trashed": f.get("trashed", False),
        }
        if entry["trashed"]:
            entry["trashed_time"] = f.get("trashedTime")
        files.append(entry)
    return {"files": files}
```

3c. `download_file` — add `trashed,trashedTime` to fields and map:

```python
async def download_file(
    service,
    file_id: str,
    export_format: Optional[str] = None,
) -> dict[str, Any]:
    metadata = await asyncio.to_thread(
        lambda: service.files()
        .get(fileId=file_id, fields="name,mimeType,size,trashed,trashedTime")
        .execute()
    )
    # ... existing download logic ...
    result = {
        "file_id": file_id,
        "file_name": metadata["name"],
        "mime_type": metadata.get("mimeType", ""),
        "size_bytes": len(content),
        "content_base64": base64.b64encode(content).decode(),
        "trashed": metadata.get("trashed", False),
    }
    if result["trashed"]:
        result["trashed_time"] = metadata.get("trashedTime")
    return result
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_drive_ops.py -v`
Expected: ALL PASS

**Step 5: Run full test suite**

Run: `uv run pytest -q`
Expected: ALL PASS

---

### Task 6: Add `trash_file` and `untrash_file` operations

**Files:**
- Modify: `src/gsuite_mcp/drive_ops.py` (add two new functions at bottom)
- Modify: `src/gsuite_mcp/server.py` (register two new tools)
- Test: `tests/test_drive_ops.py` (add tests)

**Step 1: Write the failing tests**

Add to `tests/test_drive_ops.py`:

```python
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
    # Verify update called with trashed=True
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
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_drive_ops.py::test_trash_file tests/test_drive_ops.py::test_untrash_file -v`
Expected: FAIL — functions don't exist

**Step 3: Implement trash/untrash operations**

Add to end of `src/gsuite_mcp/drive_ops.py`:

```python
async def trash_file(service, file_id: str) -> dict[str, Any]:
    """Move a file to Drive trash."""
    result = await asyncio.to_thread(
        lambda: service.files()
        .update(
            fileId=file_id,
            body={"trashed": True},
            fields="id,trashed,trashedTime",
        )
        .execute()
    )
    return {
        "file_id": result["id"],
        "trashed": result.get("trashed", True),
        "trashed_time": result.get("trashedTime"),
    }


async def untrash_file(service, file_id: str) -> dict[str, Any]:
    """Restore a file from Drive trash."""
    result = await asyncio.to_thread(
        lambda: service.files()
        .update(
            fileId=file_id,
            body={"trashed": False},
            fields="id,trashed",
        )
        .execute()
    )
    return {
        "file_id": result["id"],
        "trashed": result.get("trashed", False),
    }
```

**Step 4: Register tools in server.py**

Add before the `create_reply_draft` tool (before line 572):

```python
@mcp.tool()
async def trash_file(file_id: str) -> dict[str, Any]:
    """Move a file to Drive trash. Reversible within 30 days via untrash_file."""
    try:
        return await drive_ops.trash_file(auth.get_drive_service(), file_id)
    except HttpError as exc:
        status = exc.resp.status if exc.resp else 0
        return {
            "error": "GOOGLE_API_ERROR",
            "retryable": status in TRANSIENT_CODES,
            "http_status": status,
            "message": f"Google Drive API error (HTTP {status}): {exc}",
        }


@mcp.tool()
async def untrash_file(file_id: str) -> dict[str, Any]:
    """Restore a trashed file from Drive trash."""
    try:
        return await drive_ops.untrash_file(auth.get_drive_service(), file_id)
    except HttpError as exc:
        status = exc.resp.status if exc.resp else 0
        return {
            "error": "GOOGLE_API_ERROR",
            "retryable": status in TRANSIENT_CODES,
            "http_status": status,
            "message": f"Google Drive API error (HTTP {status}): {exc}",
        }
```

**Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_drive_ops.py -v`
Expected: ALL PASS

**Step 6: Run full test suite**

Run: `uv run pytest -q`
Expected: ALL PASS

---

### Task 7: Refuse mutations on trashed files

**Files:**
- Modify: `src/gsuite_mcp/server.py` (add trashed check to mutation tools)
- Test: `tests/test_trashed_refusal.py` (new test file)

**Step 1: Write the failing tests**

Create `tests/test_trashed_refusal.py`:

```python
"""Tests for trashed-file mutation refusal in server tool wrappers."""

import pytest
from unittest.mock import patch, MagicMock

from googleapiclient.errors import HttpError


@pytest.fixture
def trashed_meta():
    """Metadata response for a trashed Google Doc."""
    return {
        "name": "Trashed Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-05-19T00:00:00Z",
        "trashed": True,
        "trashedTime": "2026-05-06T18:00:00Z",
    }


@pytest.fixture
def live_meta():
    """Metadata response for a live Google Doc."""
    return {
        "name": "Live Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-05-19T00:00:00Z",
        "trashed": False,
    }


@pytest.fixture
def mock_drive(trashed_meta):
    with patch("gsuite_mcp.auth.get_drive_service") as mock:
        drive = MagicMock()
        mock.return_value = drive
        drive.files().get.return_value.execute.return_value = trashed_meta
        yield drive


@pytest.fixture
def mock_docs():
    with patch("gsuite_mcp.auth.get_docs_service") as mock:
        docs = MagicMock()
        mock.return_value = docs
        yield docs


@pytest.mark.asyncio
async def test_replace_text_refuses_trashed(mock_drive, mock_docs):
    from gsuite_mcp.server import replace_text
    result = await replace_text("f1", "old", "new")
    assert result["error"] == "TRASHED_FILE"
    assert result["file_id"] == "f1"
    assert "trashedTime" not in result or "trashed_time" in result


@pytest.mark.asyncio
async def test_replace_section_refuses_trashed(mock_drive, mock_docs):
    from gsuite_mcp.server import replace_section
    result = await replace_section("f1", "Heading", "New content")
    assert result["error"] == "TRASHED_FILE"


@pytest.mark.asyncio
async def test_format_document_refuses_trashed(mock_drive, mock_docs):
    from gsuite_mcp.server import format_document
    result = await format_document("f1", [{"action": "set_style", "find_text": "x", "style": "HEADING_1"}])
    assert result["error"] == "TRASHED_FILE"


@pytest.mark.asyncio
async def test_append_to_file_refuses_trashed(mock_drive, mock_docs):
    from gsuite_mcp.server import append_to_file
    result = await append_to_file("f1", "new content")
    assert result["error"] == "TRASHED_FILE"


@pytest.mark.asyncio
async def test_manage_comments_create_refuses_trashed(mock_drive, mock_docs):
    from gsuite_mcp.server import manage_comments
    result = await manage_comments("f1", "create", content="hello")
    assert result["error"] == "TRASHED_FILE"


@pytest.mark.asyncio
async def test_manage_comments_list_allows_trashed(mock_drive, mock_docs):
    """List action should work on trashed files (read-only)."""
    from gsuite_mcp.server import manage_comments
    mock_drive.comments().list.return_value.execute.return_value = {"comments": []}
    result = await manage_comments("f1", "list")
    assert "error" not in result
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_trashed_refusal.py -v`
Expected: FAIL — no trashed check exists

**Step 3: Implement trashed refusal**

Add a helper function near the top of `server.py` (after the MIME constants, around line 24):

```python
def _trashed_error(file_id: str, meta: dict) -> dict[str, Any]:
    """Return a structured TRASHED_FILE error dict."""
    return {
        "error": "TRASHED_FILE",
        "file_id": file_id,
        "file_name": meta.get("name", ""),
        "trashed_time": meta.get("trashedTime", ""),
        "retryable": False,
        "message": (
            "Cannot modify trashed file. Restore via Drive UI "
            "or call untrash_file first, or use a different file ID."
        ),
    }
```

Then add `trashed` to the `fields` strings in each mutation tool's metadata fetch, and check it. For each tool that already fetches metadata:

**`replace_text`** (line 170-173): Change fields to `"name,mimeType,modifiedTime,trashed,trashedTime"`. After the MIME check, add:
```python
    if meta.get("trashed"):
        return _trashed_error(file_id, meta)
```

**`replace_section`** (line 259-262): Same pattern.

**`format_document`** (line 339-342): Same pattern.

**`append_to_file`** (line 96-100): Change fields to `"name,mimeType,modifiedTime,trashed,trashedTime"`. After the meta fetch, add:
```python
    if meta.get("trashed"):
        return _trashed_error(file_id, meta)
```

**`manage_comments`** (line 396-428): Add a metadata fetch + trashed check for mutating actions only:
```python
    if action in ("create", "reply", "resolve"):
        meta = await asyncio.to_thread(
            lambda: drive.files()
            .get(fileId=file_id, fields="name,trashed,trashedTime")
            .execute()
        )
        if meta.get("trashed"):
            return _trashed_error(file_id, meta)
```

**`docx_suggest_edit`** (line 446-449): Change fields to `"name,mimeType,size,trashed,trashedTime"`. Add trashed check after MIME check.

**`upload_file`** (line 40-56): Add trashed check only when `file_id` is provided (update mode):
```python
    if file_id:
        drive = auth.get_drive_service()
        meta = await asyncio.to_thread(
            lambda: drive.files()
            .get(fileId=file_id, fields="name,trashed,trashedTime")
            .execute()
        )
        if meta.get("trashed"):
            return _trashed_error(file_id, meta)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_trashed_refusal.py -v`
Expected: ALL PASS

**Step 5: Run full test suite**

Run: `uv run pytest -q`
Expected: ALL PASS

---

### Task 8: Update CLAUDE.md and tool descriptions

**Files:**
- Modify: `CLAUDE.md`
- Modify: `src/gsuite_mcp/server.py` (tool docstrings for trash behavior)

**Step 1: Update CLAUDE.md**

Update tool count from 14 to 16. Add tools 15 and 16:
```
15. `trash_file` — move a file to Drive trash (reversible within 30 days)
16. `untrash_file` — restore a trashed file from Drive trash
```

Update the Key Constraints section to add:
```
- Mutation tools refuse trashed files with `error: TRASHED_FILE`. Use `untrash_file` to restore first.
- Read tools return `trashed: true` with `trashed_time` for files in Drive trash.
```

**Step 2: Update tool docstrings for trash behavior**

Add to `get_file_metadata` docstring: `"Returns trashed: true with trashed_time if the file is in Drive trash."`

Add to `replace_text`, `replace_section`, `format_document`, `append_to_file` docstrings: `"Refuses trashed files with error: TRASHED_FILE."`

**Step 3: Run full test suite**

Run: `uv run pytest -q`
Expected: ALL PASS

**Step 4: Run linter**

Run: `uv run ruff check .`
Expected: No errors

---

## Execution order summary

| Task | Description | Depends on |
|------|-------------|------------|
| 1 | Fix replace_section empty sections | — |
| 2 | Add regex match mode to format_document | — |
| 3 | Add post_styles to gdoc_template_populate | Task 2 (uses format_document) |
| 4 | Documentation updates | Tasks 1-3 |
| 5 | Add trashed field to drive_ops | — |
| 6 | Add trash_file/untrash_file | — |
| 7 | Refuse mutations on trashed files | Tasks 5-6 |
| 8 | Update CLAUDE.md and descriptions | Tasks 1-7 |

Tasks 1, 2, 5, 6 can run in parallel. Task 3 depends on 2. Task 7 depends on 5+6. Task 4 depends on 1-3. Task 8 depends on all.
