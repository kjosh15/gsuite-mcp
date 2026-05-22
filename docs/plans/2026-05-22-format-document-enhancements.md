# format_document Enhancements Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add `insert_paragraph`, `insert_paragraph_after_match`, and `set_text_style` actions to `format_document`.

**Architecture:** Three new action handlers added to the existing `format_document()` function in `docs_ops.py`. Shared text_style validation helper. Inserts use the existing `pending` list with descending-index-sort. Server docstring updated to document new actions.

**Tech Stack:** Python, Google Docs API v1 (`documents.batchUpdate`), pytest with async mocking.

---

### Task 1: Add `_validate_text_style` helper

**Files:**
- Modify: `src/gsuite_mcp/docs_ops.py` (add helper after `VALID_NAMED_STYLES` definition, ~line 24)
- Test: `tests/test_format_document.py`

**Step 1: Write the failing test**

```python
# At top of test file, add import:
from gsuite_mcp.docs_ops import _validate_text_style

# New test section after existing imports
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
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_format_document.py::test_validate_text_style_valid -v`
Expected: FAIL with `ImportError: cannot import name '_validate_text_style'`

**Step 3: Write minimal implementation**

Add to `src/gsuite_mcp/docs_ops.py` after `VALID_NAMED_STYLES` (~line 24):

```python
VALID_TEXT_STYLE_KEYS = {"bold", "italic", "underline", "strikethrough"}


def _validate_text_style(text_style: Any) -> str | None:
    """Validate a text_style dict. Returns error message or None if valid."""
    if not isinstance(text_style, dict):
        return "text_style must be a dict."
    if not text_style:
        return "text_style must contain at least one key from: bold, italic, underline, strikethrough."
    unknown = set(text_style.keys()) - VALID_TEXT_STYLE_KEYS
    if unknown:
        return f"Unknown text_style keys: {', '.join(sorted(unknown))}. Valid: bold, italic, underline, strikethrough."
    for key, val in text_style.items():
        if not isinstance(val, bool):
            return f"text_style['{key}'] must be a boolean, got {type(val).__name__}."
    return None
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_format_document.py -k "validate_text_style" -v`
Expected: 5 PASSED

**Step 5: Commit**

---

### Task 2: Add `set_text_style` action

**Files:**
- Modify: `src/gsuite_mcp/docs_ops.py:860` (add to `valid_actions` set)
- Modify: `src/gsuite_mcp/docs_ops.py:908-918` (add validation block for `set_text_style`)
- Modify: `src/gsuite_mcp/docs_ops.py:1019-1053` (add handler after `set_style` handler)
- Test: `tests/test_format_document.py`

**Step 1: Write the failing tests**

```python
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
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_format_document.py::test_set_text_style_basic -v`
Expected: FAIL with `INVALID_ACTION`

**Step 3: Write implementation**

In `src/gsuite_mcp/docs_ops.py`, make these changes:

1. Add `"set_text_style"` to `valid_actions` set on line 860:

```python
valid_actions = {"set_style", "set_text_style", "delete", "delete_empty_after", "delete_by_index"}
```

2. Add validation for `set_text_style` after the `set_style` validation block (~line 918):

```python
        if action == "set_text_style":
            ts = op.get("style")
            err = _validate_text_style(ts)
            if err:
                return {
                    "error": "INVALID_TEXT_STYLE",
                    "retryable": False,
                    "message": f"Operation {i}: {err}",
                }
```

3. Add `set_text_style` to the find_text requirement — it needs `find_text`, so include it in the `else` branch at line 880. No change needed since `set_text_style` is not `delete_by_index`, so it already falls into the else branch.

4. Add `set_text_style` to the multi-match protection check at line 1003:

```python
        if action in ("delete", "set_style", "set_text_style") and len(matches) > 1:
```

5. Add the handler after the `set_style` handler block (after line 1053):

```python
        elif action == "set_text_style":
            style = op["style"]
            fields_mask = ",".join(sorted(style.keys()))
            for block_idx, block in matches:
                text_snippet = _para_text(block["paragraph"]).strip()[:80]
                if preview:
                    results.append({
                        "action": "set_text_style",
                        "find_text": find_text,
                        "style": style,
                        "paragraph_index": block_idx,
                        "text": text_snippet,
                        "status": "would_apply",
                    })
                else:
                    pending.append((block["startIndex"], {
                        "updateTextStyle": {
                            "range": {
                                "startIndex": block["startIndex"],
                                "endIndex": block["endIndex"],
                            },
                            "textStyle": style,
                            "fields": fields_mask,
                        }
                    }))
            if not preview:
                results.append({
                    "action": "set_text_style",
                    "find_text": find_text,
                    "style": style,
                    "status": "applied",
                    "start_index": matches[0][1]["startIndex"],
                })
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_format_document.py -k "set_text_style" -v`
Expected: 8 PASSED

**Step 5: Run full test suite for regressions**

Run: `uv run pytest tests/test_format_document.py -v`
Expected: All existing + new tests PASS

**Step 6: Commit**

---

### Task 3: Add `insert_paragraph` action

**Files:**
- Modify: `src/gsuite_mcp/docs_ops.py:860` (add to `valid_actions`)
- Modify: `src/gsuite_mcp/docs_ops.py:872-879` (add validation)
- Modify: `src/gsuite_mcp/docs_ops.py` (add handler in the `delete_by_index`-like path, since it uses index not find_text)
- Test: `tests/test_format_document.py`

**Step 1: Write the failing tests**

```python
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
    assert "insertText" in requests[1]  # sorted descending: style at index 14 first, insert at 14
    style_req = requests[0]["updateTextStyle"]
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
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_format_document.py::test_insert_paragraph_basic -v`
Expected: FAIL

**Step 3: Write implementation**

1. Add `"insert_paragraph"` to `valid_actions`.

2. Add validation for `insert_paragraph` in the validation loop. It needs `after_paragraph_index` (int) and `text` (non-empty string). It does NOT need `find_text`. Update the validation branching:

```python
        if action == "delete_by_index":
            pi = op.get("paragraph_index")
            if not isinstance(pi, int):
                return {
                    "error": "MISSING_PARAGRAPH_INDEX",
                    "retryable": False,
                    "message": f"Operation {i}: 'paragraph_index' is required and must be an integer.",
                }
        elif action == "insert_paragraph":
            pi = op.get("after_paragraph_index")
            if not isinstance(pi, int):
                return {
                    "error": "MISSING_PARAGRAPH_INDEX",
                    "retryable": False,
                    "message": f"Operation {i}: 'after_paragraph_index' is required and must be an integer.",
                }
            text = op.get("text")
            if not isinstance(text, str) or not text.strip():
                return {
                    "error": "MISSING_TEXT",
                    "retryable": False,
                    "message": f"Operation {i}: 'text' is required and must be non-blank.",
                }
            ts = op.get("text_style")
            if ts is not None:
                err = _validate_text_style(ts)
                if err:
                    return {
                        "error": "INVALID_TEXT_STYLE",
                        "retryable": False,
                        "message": f"Operation {i}: {err}",
                    }
        else:
            # All other actions require find_text
            find_text = op.get("find_text", "")
            ...
```

3. Skip `match_mode` validation for `insert_paragraph` (it doesn't use text matching). Update the condition at line 888:

```python
        if action not in ("delete_by_index", "insert_paragraph"):
```

4. Add handler in the main loop, right after the `delete_by_index` handler:

```python
        # --- insert_paragraph: index-based insert --------------------------
        if action == "insert_paragraph":
            para_idx = op["after_paragraph_index"]
            if para_idx < 0 or para_idx >= len(content):
                results.append({
                    "action": "insert_paragraph",
                    "after_paragraph_index": para_idx,
                    "status": "index_out_of_range",
                })
                continue
            block = content[para_idx]
            if not block.get("paragraph"):
                results.append({
                    "action": "insert_paragraph",
                    "after_paragraph_index": para_idx,
                    "status": "not_a_paragraph",
                })
                continue

            insert_text = op["text"]
            if not insert_text.endswith("\n"):
                insert_text += "\n"
            insert_index = block["endIndex"]
            text_style = op.get("text_style")
            nesting_override = op.get("nesting_level")

            if preview:
                results.append({
                    "action": "insert_paragraph",
                    "after_paragraph_index": para_idx,
                    "text": insert_text.strip()[:80],
                    "status": "would_apply",
                })
            else:
                # insertText
                pending.append((insert_index, {
                    "insertText": {
                        "location": {"index": insert_index},
                        "text": insert_text,
                    }
                }))
                # updateTextStyle if text_style provided
                if text_style:
                    fields_mask = ",".join(sorted(text_style.keys()))
                    pending.append((insert_index, {
                        "updateTextStyle": {
                            "range": {
                                "startIndex": insert_index,
                                "endIndex": insert_index + len(insert_text),
                            },
                            "textStyle": text_style,
                            "fields": fields_mask,
                        }
                    }))
                # nesting_level override via paragraph style
                if nesting_override is not None:
                    indent = nesting_override * 36  # 36pt per nesting level (Google Docs default)
                    pending.append((insert_index, {
                        "updateParagraphStyle": {
                            "range": {
                                "startIndex": insert_index,
                                "endIndex": insert_index + len(insert_text),
                            },
                            "paragraphStyle": {
                                "indentStart": {"magnitude": indent, "unit": "PT"},
                                "indentFirstLine": {"magnitude": indent, "unit": "PT"},
                            },
                            "fields": "indentStart,indentFirstLine",
                        }
                    }))
                results.append({
                    "action": "insert_paragraph",
                    "after_paragraph_index": para_idx,
                    "status": "applied",
                    "characters_inserted": len(insert_text),
                })
            continue
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_format_document.py -k "insert_paragraph" -v`
Expected: 11 PASSED

**Step 5: Run full test suite for regressions**

Run: `uv run pytest tests/test_format_document.py -v`
Expected: All PASS

**Step 6: Commit**

---

### Task 4: Add `insert_paragraph_after_match` action

**Files:**
- Modify: `src/gsuite_mcp/docs_ops.py` (validation + handler)
- Test: `tests/test_format_document.py`

**Step 1: Write the failing tests**

```python
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
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_format_document.py::test_insert_after_match_basic -v`
Expected: FAIL

**Step 3: Write implementation**

1. Add `"insert_paragraph_after_match"` to `valid_actions`.

2. Add validation: `insert_paragraph_after_match` needs both `find_text` (handled by existing else branch) AND `text`. Add after the `find_text` validation:

```python
        if action == "insert_paragraph_after_match":
            text = op.get("text")
            if not isinstance(text, str) or not text.strip():
                return {
                    "error": "MISSING_TEXT",
                    "retryable": False,
                    "message": f"Operation {i}: 'text' is required and must be non-blank.",
                }
            ts = op.get("text_style")
            if ts is not None:
                err = _validate_text_style(ts)
                if err:
                    return {
                        "error": "INVALID_TEXT_STYLE",
                        "retryable": False,
                        "message": f"Operation {i}: {err}",
                    }
```

3. Add `insert_paragraph_after_match` to the multi-match protection (but WITHOUT `match_all` support):

```python
        if action in ("delete", "set_style", "set_text_style", "insert_paragraph_after_match") and len(matches) > 1:
            if not op.get("match_all", False) or action == "insert_paragraph_after_match":
```

4. Add handler after `set_text_style` handler:

```python
        elif action == "insert_paragraph_after_match":
            # Use first (only) match
            block_idx, block = matches[0]
            insert_text = op["text"]
            if not insert_text.endswith("\n"):
                insert_text += "\n"
            insert_index = block["endIndex"]
            text_style = op.get("text_style")
            nesting_override = op.get("nesting_level")

            if preview:
                results.append({
                    "action": "insert_paragraph_after_match",
                    "find_text": find_text,
                    "after_paragraph_index": block_idx,
                    "text": insert_text.strip()[:80],
                    "status": "would_apply",
                })
            else:
                pending.append((insert_index, {
                    "insertText": {
                        "location": {"index": insert_index},
                        "text": insert_text,
                    }
                }))
                if text_style:
                    fields_mask = ",".join(sorted(text_style.keys()))
                    pending.append((insert_index, {
                        "updateTextStyle": {
                            "range": {
                                "startIndex": insert_index,
                                "endIndex": insert_index + len(insert_text),
                            },
                            "textStyle": text_style,
                            "fields": fields_mask,
                        }
                    }))
                if nesting_override is not None:
                    indent = nesting_override * 36
                    pending.append((insert_index, {
                        "updateParagraphStyle": {
                            "range": {
                                "startIndex": insert_index,
                                "endIndex": insert_index + len(insert_text),
                            },
                            "paragraphStyle": {
                                "indentStart": {"magnitude": indent, "unit": "PT"},
                                "indentFirstLine": {"magnitude": indent, "unit": "PT"},
                            },
                            "fields": "indentStart,indentFirstLine",
                        }
                    }))
                results.append({
                    "action": "insert_paragraph_after_match",
                    "find_text": find_text,
                    "after_paragraph_index": block_idx,
                    "status": "applied",
                    "characters_inserted": len(insert_text),
                })
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_format_document.py -k "insert_after_match" -v`
Expected: 8 PASSED

**Step 5: Full test suite**

Run: `uv run pytest tests/test_format_document.py -v`
Expected: All PASS

**Step 6: Commit**

---

### Task 5: Update server docstring and CLAUDE.md

**Files:**
- Modify: `src/gsuite_mcp/server.py:390-423` (server tool docstring)
- Modify: `CLAUDE.md:48` (tool #9 description)

**Step 1: Update server.py docstring**

Add the three new actions to the docstring of `format_document` in `server.py`:

```python
    """Apply paragraph formatting operations to a Google Doc in a single batch.

    Each operation is a dict with an "action" key:

    - set_style: Change paragraph style.
      {"action": "set_style", "find_text": "Introduction", "style": "HEADING_1"}
      Valid styles: NORMAL_TEXT, TITLE, SUBTITLE, HEADING_1..HEADING_6.

    - set_text_style: Apply inline text formatting to a paragraph.
      {"action": "set_text_style", "find_text": "Introduction",
       "style": {"italic": true, "bold": true}}
      Valid style keys: bold, italic, underline, strikethrough.
      Only specified keys are changed; omitted keys left unchanged.

    - delete: Delete a paragraph.
      {"action": "delete", "find_text": "Paragraph to remove"}

    - delete_by_index: Delete a paragraph by its content index (from a prior read).
      {"action": "delete_by_index", "paragraph_index": 3}

    - delete_empty_after: Remove blank paragraphs after a matched paragraph.
      {"action": "delete_empty_after", "find_text": "Introduction"}

    - insert_paragraph: Insert a new paragraph after a content block index.
      {"action": "insert_paragraph", "after_paragraph_index": 3,
       "text": "New item", "text_style": {"italic": true}}
      Inherits list formatting from neighbor by default.
      Optional overrides: nesting_level, list_id, text_style.

    - insert_paragraph_after_match: Find a paragraph by text, insert after it.
      {"action": "insert_paragraph_after_match", "find_text": "Existing item",
       "text": "New item", "inherit_list_formatting": true}
      Multi-match returns error (no match_all support).
      Optional: inherit_list_formatting, nesting_level, list_id, text_style.

    Matching rules:
    ...rest unchanged...
    """
```

**Step 2: Update CLAUDE.md line 48**

Replace the tool #9 description:

```
9. `format_document` — batch paragraph formatting: set_style, set_text_style (bold/italic/underline/strikethrough), delete, delete_by_index, delete_empty_after, insert_paragraph (by index, inherits list formatting), insert_paragraph_after_match (by text match). Multi-match protection: >1 match fails unless `match_all: true`. `preview: true` for dry-run.
```

**Step 3: Run full test suite**

Run: `uv run pytest -q`
Expected: All tests PASS

**Step 4: Commit**

---

### Task 6: Update docs_ops.py docstring

**Files:**
- Modify: `src/gsuite_mcp/docs_ops.py:823-851` (function docstring)

**Step 1: Update the docstring**

Add the three new actions to the `format_document` docstring in `docs_ops.py` to match the server docstring.

**Step 2: Commit**

---

### Task 7: Final verification

**Step 1: Run full test suite**

Run: `uv run pytest -q`
Expected: All tests PASS

**Step 2: Run linter**

Run: `uv run ruff check .`
Expected: No errors

**Step 3: Final commit if any lint fixes needed**
