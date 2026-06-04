# Mutation Safety Guardrails — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add 6 safety guardrails to prevent destructive edits, motivated by a `replace_section` incident that silently deleted 6,653 chars.

**Architecture:** Shared blast-radius helper in `docs_ops.py`, new `create_backup_copy` in `drive_ops.py`, wiring in `server.py` and `gdoc_ops.py`. All changes are additive parameters with backward-compatible defaults except `gdoc_batch_replace`'s `expected_count` defaulting to 1 (breaking).

**Tech Stack:** Python 3.12, pytest, FastMCP, Google Docs/Drive API v3

**Design doc:** `docs/plans/2026-06-04-mutation-safety-guardrails-design.md`

**Current test count:** 245 passed, 2 skipped

---

## Task 1: Blast-radius guard helper

Shared pure function that decides whether a mutation's char delta is dangerous.

**Files:**
- Create: `tests/test_blast_radius.py`
- Modify: `src/gsuite_mcp/docs_ops.py` (add `check_blast_radius` near top)

### Step 1: Write the failing test

```python
# tests/test_blast_radius.py
"""Tests for the blast-radius guard helper."""

import pytest
from gsuite_mcp.docs_ops import check_blast_radius


class TestCheckBlastRadius:
    def test_small_edit_passes(self):
        """Small delta, small ratio — no guard trip."""
        result = check_blast_radius(chars_deleted=50, chars_inserted=30)
        assert result is None

    def test_large_delta_and_ratio_trips(self):
        """Deleted - inserted > 200 AND deleted > 2x inserted — trips guard."""
        result = check_blast_radius(chars_deleted=6653, chars_inserted=201)
        assert result is not None
        assert result["error"] == "BLAST_RADIUS_EXCEEDED"
        assert result["chars_deleted"] == 6653
        assert result["chars_inserted"] == 201
        assert result["net_change"] == 201 - 6653
        assert "confirm_delete_chars=6653" in result["message"]
        assert result["retryable"] is True

    def test_large_delta_but_small_ratio_passes(self):
        """Deleted - inserted > 200, but deleted < 2x inserted — passes."""
        result = check_blast_radius(chars_deleted=500, chars_inserted=400)
        assert result is None

    def test_large_ratio_but_small_delta_passes(self):
        """Deleted > 2x inserted, but delta < 200 — passes."""
        result = check_blast_radius(chars_deleted=100, chars_inserted=10)
        assert result is None

    def test_pure_deletion_trips(self):
        """Deleting 300 chars and inserting 0 always trips."""
        result = check_blast_radius(chars_deleted=300, chars_inserted=0)
        assert result is not None
        assert result["error"] == "BLAST_RADIUS_EXCEEDED"

    def test_confirm_bypasses_guard(self):
        """Passing correct confirm_delete_chars bypasses the guard."""
        result = check_blast_radius(
            chars_deleted=6653, chars_inserted=201,
            confirm_delete_chars=6653,
        )
        assert result is None

    def test_confirm_wrong_value_still_trips(self):
        """Wrong confirm_delete_chars value does not bypass."""
        result = check_blast_radius(
            chars_deleted=6653, chars_inserted=201,
            confirm_delete_chars=999,
        )
        assert result is not None
        assert result["error"] == "BLAST_RADIUS_EXCEEDED"

    def test_custom_thresholds(self):
        """Custom min_delta and max_ratio override defaults."""
        # Would trip default (delta=250>200, ratio=5>2) but not custom
        result = check_blast_radius(
            chars_deleted=300, chars_inserted=50,
            min_delta=500, max_ratio=10,
        )
        assert result is None

    def test_equal_delete_insert_passes(self):
        """Same chars deleted and inserted — no guard trip."""
        result = check_blast_radius(chars_deleted=5000, chars_inserted=5000)
        assert result is None
```

### Step 2: Run test to verify it fails

Run: `uv run pytest tests/test_blast_radius.py -v`
Expected: FAIL with `ImportError: cannot import name 'check_blast_radius'`

### Step 3: Write minimal implementation

Add to `src/gsuite_mcp/docs_ops.py` after the `_FALLBACK_RANK` constant (around line 17):

```python
# ---------------------------------------------------------------------------
# Blast-radius guard
# ---------------------------------------------------------------------------

_BLAST_RADIUS_MIN_DELTA: int = 200
_BLAST_RADIUS_MAX_RATIO: float = 2.0


def check_blast_radius(
    *,
    chars_deleted: int,
    chars_inserted: int,
    confirm_delete_chars: int | None = None,
    min_delta: int = _BLAST_RADIUS_MIN_DELTA,
    max_ratio: float = _BLAST_RADIUS_MAX_RATIO,
) -> dict[str, Any] | None:
    """Return an error dict if the edit exceeds the blast-radius threshold.

    Returns None if the edit is safe or confirmed.
    Both conditions must hold to trip the guard:
      1. chars_deleted - chars_inserted > min_delta
      2. chars_deleted > chars_inserted * max_ratio
    Passing confirm_delete_chars == chars_deleted bypasses the guard.
    """
    delta = chars_deleted - chars_inserted
    ratio_exceeded = (
        chars_inserted == 0 or chars_deleted > chars_inserted * max_ratio
    )

    if delta > min_delta and ratio_exceeded:
        if confirm_delete_chars == chars_deleted:
            return None
        return {
            "error": "BLAST_RADIUS_EXCEEDED",
            "retryable": True,
            "chars_deleted": chars_deleted,
            "chars_inserted": chars_inserted,
            "net_change": chars_inserted - chars_deleted,
            "message": (
                f"Deletion exceeds safety threshold. "
                f"Pass confirm_delete_chars={chars_deleted} to proceed."
            ),
        }
    return None
```

### Step 4: Run test to verify it passes

Run: `uv run pytest tests/test_blast_radius.py -v`
Expected: All 9 tests PASS

### Step 5: Commit

Auto-committed by hook.

---

## Task 2: replace_section — dry_run parameter

**Files:**
- Modify: `tests/test_replace_section.py`
- Modify: `src/gsuite_mcp/docs_ops.py:198-349` (`replace_section`)
- Modify: `src/gsuite_mcp/server.py:316-381` (server wrapper)

### Step 1: Write the failing tests

Add to `tests/test_replace_section.py`:

```python
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
    # batchUpdate must NOT be called
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
```

### Step 2: Run tests to verify they fail

Run: `uv run pytest tests/test_replace_section.py::test_replace_section_dry_run_returns_span tests/test_replace_section.py::test_replace_section_dry_run_include_heading -v`
Expected: FAIL with `TypeError: replace_section() got an unexpected keyword argument 'dry_run'`

### Step 3: Implement dry_run in docs_ops.replace_section

Modify `src/gsuite_mcp/docs_ops.py` `replace_section` function signature (line 198) to add `dry_run: bool = False`:

```python
async def replace_section(
    docs_service,
    file_id: str,
    section_heading: str,
    new_content: str,
    include_heading: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
```

Then, right before the `if empty_section:` block (line 263), add early return for dry_run. The key change: instead of executing batchUpdate, return the computed values. Insert after `characters_inserted = len(new_content)` (line 261):

```python
    if dry_run:
        return {
            "dry_run": True,
            "file_id": file_id,
            "section_heading": heading["text"],
            "heading_level": heading["heading_level"],
            "chars_deleted": delete_end - delete_start if not (delete_start >= delete_end and not include_heading) else 0,
            "chars_inserted": characters_inserted,
            "section_span": {
                "start_index": delete_start,
                "end_index": delete_end,
            },
            "include_heading": include_heading,
        }
```

Actually, that logic is cleaner computed after the empty_section check. Let me restructure. The cleanest approach: compute `characters_deleted` and `section_span` BEFORE branching on empty_section vs normal, then check dry_run:

After `delete_end = section_end` and `empty_section = ...`:

```python
    # Compute metadata used by dry_run and response
    if empty_section:
        characters_deleted = 0
        section_span = {"start_index": heading["end_index"], "end_index": heading["end_index"]}
    else:
        characters_deleted = delete_end - delete_start
        section_span = {"start_index": delete_start, "end_index": delete_end}

    if dry_run:
        return {
            "dry_run": True,
            "file_id": file_id,
            "section_heading": heading["text"],
            "heading_level": heading["heading_level"],
            "chars_deleted": characters_deleted,
            "chars_inserted": characters_inserted,
            "section_span": section_span,
            "include_heading": include_heading,
        }
```

Then update the server wrapper (`server.py:316`) to pass `dry_run`:

```python
@mcp.tool()
async def replace_section(
    file_id: str,
    section_heading: str,
    new_content: str,
    include_heading: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
```

And in the body, pass it through:

```python
    result = await docs_ops.replace_section(
        docs, file_id, section_heading, new_content, include_heading,
        dry_run=dry_run,
    )
```

Skip modified_time fetch when dry_run:

```python
    if "error" in result:
        return result
    if not result.get("dry_run"):
        meta2 = await asyncio.to_thread(...)
        result["modified_time"] = meta2.get("modifiedTime", "")
    return result
```

### Step 4: Run tests to verify they pass

Run: `uv run pytest tests/test_replace_section.py -v`
Expected: All tests PASS (existing + 2 new)

### Step 5: Commit

Auto-committed by hook.

---

## Task 3: replace_section — expected_delete_chars + NORMAL_TEXT flagging

**Files:**
- Modify: `tests/test_replace_section.py`
- Modify: `src/gsuite_mcp/docs_ops.py:198+` (`replace_section`)
- Modify: `src/gsuite_mcp/server.py:316+` (server wrapper)

### Step 1: Write the failing tests

Add to `tests/test_replace_section.py`:

```python
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
```

### Step 2: Run tests to verify they fail

Run: `uv run pytest tests/test_replace_section.py::test_replace_section_expected_delete_chars_match tests/test_replace_section.py::test_replace_section_expected_delete_chars_mismatch tests/test_replace_section.py::test_replace_section_normal_text_anchor_flagged -v`
Expected: FAIL — `TypeError` for `expected_delete_chars` kwarg, and missing keys in result

### Step 3: Implement expected_delete_chars + NORMAL_TEXT flagging

**In `docs_ops.py` `replace_section`:**

Add parameter:
```python
async def replace_section(
    docs_service,
    file_id: str,
    section_heading: str,
    new_content: str,
    include_heading: bool = False,
    dry_run: bool = False,
    expected_delete_chars: int | None = None,
) -> dict[str, Any]:
```

After computing `characters_deleted` and `section_span`, compute anchor flags:

```python
    # Anchor metadata
    anchor_is_styled_heading = heading["heading_level"] in _HEADING_RANKS
    # section_extends_to: flag when section runs to end-of-document
    last_end = content[-1]["endIndex"] if content else 0
    section_extends_to_end = (delete_end >= last_end)
```

Add expected_delete_chars check (after computing characters_deleted, before dry_run check):

```python
    if expected_delete_chars is not None and characters_deleted != expected_delete_chars:
        return {
            "error": "DELETE_CHARS_MISMATCH",
            "retryable": True,
            "expected": expected_delete_chars,
            "actual": characters_deleted,
            "section_span": section_span,
            "message": (
                f"Expected to delete {expected_delete_chars} chars but section "
                f"contains {characters_deleted}. Use dry_run=true to inspect."
            ),
        }
```

Add anchor flags to ALL return paths (dry_run, normal, empty_section):

```python
    # Base response fields
    anchor_fields = {"anchor_is_styled_heading": anchor_is_styled_heading}
    if not anchor_is_styled_heading and section_extends_to_end:
        anchor_fields["section_extends_to"] = "END_OF_DOCUMENT"
```

Merge into each return dict.

**In `server.py`:** Add `expected_delete_chars: Optional[int] = None` parameter and pass through.

### Step 4: Run tests

Run: `uv run pytest tests/test_replace_section.py -v`
Expected: All PASS

### Step 5: Commit

Auto-committed by hook.

---

## Task 4: replace_section — blast-radius guard integration + confirm_delete_chars

**Files:**
- Modify: `tests/test_replace_section.py`
- Modify: `src/gsuite_mcp/docs_ops.py` (`replace_section`)
- Modify: `src/gsuite_mcp/server.py`

### Step 1: Write the failing tests

Add to `tests/test_replace_section.py`:

```python
@pytest.mark.asyncio
async def test_replace_section_blast_radius_trips():
    """Large deletion without confirm trips blast-radius guard."""
    # Section body is 6653 chars, new content is 201 chars
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
```

### Step 2: Run tests to verify they fail

Run: `uv run pytest tests/test_replace_section.py::test_replace_section_blast_radius_trips tests/test_replace_section.py::test_replace_section_blast_radius_confirmed -v`
Expected: FAIL — `TypeError` for `confirm_delete_chars`, no BLAST_RADIUS_EXCEEDED error returned

### Step 3: Implement

Add `confirm_delete_chars: int | None = None` parameter to `docs_ops.replace_section`.

After expected_delete_chars check and before the dry_run early return, add:

```python
    # Blast-radius guard (skipped on dry_run — dry_run shows the info without blocking)
    if not dry_run:
        blast = check_blast_radius(
            chars_deleted=characters_deleted,
            chars_inserted=characters_inserted,
            confirm_delete_chars=confirm_delete_chars,
        )
        if blast is not None:
            blast["section_span"] = section_span
            blast.update(anchor_fields)
            return blast
```

Add `confirm_delete_chars: Optional[int] = None` to server wrapper and pass through.

Read env vars in `server.py` and pass as `min_delta`/`max_ratio` if you want env-var support. Or just use defaults for now and add env-var wiring in Task 7.

### Step 4: Run tests

Run: `uv run pytest tests/test_replace_section.py -v`
Expected: All PASS

### Step 5: Commit

Auto-committed by hook.

---

## Task 5: gdoc_batch_replace — default expected_count to 1

**Files:**
- Modify: `tests/test_gdoc_batch_replace.py`
- Modify: `src/gsuite_mcp/docs_ops.py:1425+` (`batch_replace`)

### Step 1: Write the failing tests

Add to `tests/test_gdoc_batch_replace.py`:

```python
@pytest.mark.asyncio
async def test_batch_replace_default_expected_count_1():
    """Omitting expected_count now defaults to 1 (not None)."""
    docs = MagicMock()
    doc = _make_doc("Hello Hello Hello")  # 3 occurrences
    docs.documents().get.return_value.execute.return_value = doc

    from gsuite_mcp.docs_ops import batch_replace
    result = await batch_replace(
        docs, "file1",
        edits=[{"find_text": "Hello", "replace_text": "Hi"}],
    )

    # 3 matches but default expected_count=1 → count_mismatch
    assert result["committed"] is False
    assert result["results"][0]["status"] == "count_mismatch"
    assert result["results"][0]["matches_found"] == 3
    assert result["results"][0]["expected_count"] == 1


@pytest.mark.asyncio
async def test_batch_replace_explicit_count_overrides_default():
    """Explicit expected_count overrides the default of 1."""
    docs = MagicMock()
    doc = _make_doc("Hello Hello Hello")
    docs.documents().get.return_value.execute.return_value = doc
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    from gsuite_mcp.docs_ops import batch_replace
    result = await batch_replace(
        docs, "file1",
        edits=[{"find_text": "Hello", "replace_text": "Hi", "expected_count": 3}],
    )
    assert result["committed"] is True
    assert result["results"][0]["status"] == "ok"


@pytest.mark.asyncio
async def test_batch_replace_explicit_none_disables_count_check():
    """Passing expected_count=None explicitly disables the count check."""
    docs = MagicMock()
    doc = _make_doc("Hello Hello Hello")
    docs.documents().get.return_value.execute.return_value = doc
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    from gsuite_mcp.docs_ops import batch_replace
    result = await batch_replace(
        docs, "file1",
        edits=[{"find_text": "Hello", "replace_text": "Hi", "expected_count": None}],
    )
    assert result["committed"] is True
```

### Step 2: Run tests to verify they fail

Run: `uv run pytest tests/test_gdoc_batch_replace.py::test_batch_replace_default_expected_count_1 -v`
Expected: FAIL — currently omitting expected_count means "replace all", so commit succeeds

### Step 3: Implement

In `src/gsuite_mcp/docs_ops.py` `batch_replace` (line ~1454), change:

```python
        expected_count = edit.get("expected_count")
```

to:

```python
        # Default to 1 if not specified (breaking change from None).
        # Callers must pass expected_count=N for multi-match or None to disable.
        expected_count = edit.get("expected_count", 1)
```

**Important:** Also update the existing test `test_batch_replace_happy_path` (and `test_batch_replace_zero_matches_no_error`, `test_batch_replace_cross_paragraph`) to explicitly pass `expected_count` since the default changed. These tests call with 1 match per pair, so they still pass. But `test_batch_replace_zero_matches_no_error` has `NOTFOUND` with 0 matches — that will now fail with default `expected_count=1`. Fix: pass `expected_count=None` for the NOTFOUND edit, or `expected_count=0`.

Actually, let me check: `test_batch_replace_zero_matches_no_error` has:
```python
edits=[
    {"find_text": "Hello", "replace_text": "Hi"},        # 1 match, default expected=1 → ok
    {"find_text": "NOTFOUND", "replace_text": "x"},      # 0 matches, default expected=1 → MISMATCH
]
```

This existing test will break. We need to update it to pass `expected_count=None` for the zero-match case. Similarly check all existing tests.

Tests to update:
- `test_batch_replace_happy_path` — has 2 edits, each matching once → OK with default 1
- `test_batch_replace_dry_run` — "Hello" appears 2x → will now mismatch. Fix: add `expected_count=2` or `expected_count=None`
- `test_batch_replace_zero_matches_no_error` — second edit matches 0 → will mismatch. Fix: add `expected_count=None`
- `test_batch_replace_all_zero_matches_no_commit` — matches 0, default 1 → mismatch. Fix: add `expected_count=None`
- `test_batch_replace_reverse_order_requests` — "AAA" appears 2x → will mismatch. Fix: add `expected_count=2`
- `test_pure_deletion_single_pair` — 1 match → OK
- `test_pure_deletion_in_mixed_batch` — 1 match each → OK
- `test_deletion_dry_run_matches_commit` — 1 match → OK
- `test_deletion_preserves_overlap_guard` — 1 match each → OK (overlap fires first anyway)

Update these tests to be explicit about expected_count.

### Step 4: Run tests

Run: `uv run pytest tests/test_gdoc_batch_replace.py -v`
Expected: All PASS

### Step 5: Commit

Auto-committed by hook.

---

## Task 6: gdoc_batch_replace — blast-radius guard + diff summary

**Files:**
- Modify: `tests/test_gdoc_batch_replace.py`
- Modify: `src/gsuite_mcp/docs_ops.py` (`batch_replace`)
- Modify: `src/gsuite_mcp/server.py` (add `confirm_delete_chars` param)

### Step 1: Write the failing tests

Add to `tests/test_gdoc_batch_replace.py`:

```python
@pytest.mark.asyncio
async def test_batch_replace_blast_radius_trips():
    """Large net deletion without confirm trips blast-radius guard."""
    docs = MagicMock()
    # Build doc with a long paragraph (500 chars) to be replaced with short text
    long_text = "x" * 499  # + \n = 500 chars in doc
    doc = _make_doc(long_text)
    docs.documents().get.return_value.execute.return_value = doc
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    from gsuite_mcp.docs_ops import batch_replace
    result = await batch_replace(
        docs, "file1",
        edits=[{"find_text": long_text, "replace_text": "y", "expected_count": 1}],
    )
    assert result["error"] == "BLAST_RADIUS_EXCEEDED"
    assert result["chars_deleted"] == 499
    assert result["chars_inserted"] == 1
    assert result["retryable"] is True
    docs.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_batch_replace_blast_radius_confirmed():
    """confirm_delete_chars bypasses blast-radius guard."""
    docs = MagicMock()
    long_text = "x" * 499
    doc = _make_doc(long_text)
    docs.documents().get.return_value.execute.return_value = doc
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    from gsuite_mcp.docs_ops import batch_replace
    result = await batch_replace(
        docs, "file1",
        edits=[{"find_text": long_text, "replace_text": "y", "expected_count": 1}],
        confirm_delete_chars=499,
    )
    assert result["committed"] is True
    docs.documents().batchUpdate.assert_called_once()


@pytest.mark.asyncio
async def test_batch_replace_diff_summary():
    """Response includes chars_deleted, chars_inserted, net_change."""
    docs = MagicMock()
    doc = _make_doc("Hello world")
    docs.documents().get.return_value.execute.return_value = doc
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    from gsuite_mcp.docs_ops import batch_replace
    result = await batch_replace(
        docs, "file1",
        edits=[{"find_text": "Hello", "replace_text": "Hi", "expected_count": 1}],
    )
    assert result["chars_deleted"] == 5  # len("Hello")
    assert result["chars_inserted"] == 2  # len("Hi")
    assert result["net_change"] == -3
```

### Step 2: Run tests to verify they fail

Run: `uv run pytest tests/test_gdoc_batch_replace.py::test_batch_replace_blast_radius_trips tests/test_gdoc_batch_replace.py::test_batch_replace_diff_summary -v`
Expected: FAIL — no `error` key, no `chars_deleted` key in result

### Step 3: Implement

In `docs_ops.py` `batch_replace`, add `confirm_delete_chars: int | None = None` parameter:

```python
async def batch_replace(
    docs_service,
    file_id: str,
    edits: list[dict],
    dry_run: bool = False,
    confirm_delete_chars: int | None = None,
) -> dict[str, Any]:
```

After Phase 2 (overlap check) and before the dry_run/total==0 check, compute aggregate char counts:

```python
    # Compute aggregate chars_deleted / chars_inserted
    total_chars_deleted = 0
    total_chars_inserted = 0
    for pair_idx, matches in enumerate(all_pair_matches):
        find_len = len(edits[pair_idx]["find_text"])
        replace_len = len(edits[pair_idx]["replace_text"])
        total_chars_deleted += find_len * len(matches)
        total_chars_inserted += replace_len * len(matches)
```

Add diff_summary to all return paths:

```python
    diff_summary = {
        "chars_deleted": total_chars_deleted,
        "chars_inserted": total_chars_inserted,
        "net_change": total_chars_inserted - total_chars_deleted,
    }
```

Add blast-radius guard before the dry_run check:

```python
    if not dry_run and total > 0:
        blast = check_blast_radius(
            chars_deleted=total_chars_deleted,
            chars_inserted=total_chars_inserted,
            confirm_delete_chars=confirm_delete_chars,
        )
        if blast is not None:
            blast["results"] = results
            blast["committed"] = False
            blast["total_replacements"] = 0
            return blast
```

Merge `diff_summary` into both the dry_run return and the committed return.

In `server.py`, add `confirm_delete_chars: Optional[int] = None` to `gdoc_batch_replace` tool and pass through to `gdoc_ops.batch_replace`, which passes through to `docs_ops.batch_replace`.

Also update `gdoc_ops.py` `batch_replace` to accept and pass `confirm_delete_chars`:

```python
async def batch_replace(
    drive_service,
    docs_service,
    file_id: str,
    edits: list[dict],
    dry_run: bool = False,
    confirm_delete_chars: int | None = None,
) -> dict[str, Any]:
    ...
    result = await docs_ops.batch_replace(
        docs_service, file_id, edits, dry_run=dry_run,
        confirm_delete_chars=confirm_delete_chars,
    )
```

### Step 4: Run tests

Run: `uv run pytest tests/test_gdoc_batch_replace.py -v`
Expected: All PASS

### Step 5: Commit

Auto-committed by hook.

---

## Task 7: Auto-snapshot on blast-radius trips (drive_ops helper)

**Files:**
- Create: `tests/test_backup_copy.py`
- Modify: `src/gsuite_mcp/drive_ops.py` (add `create_backup_copy`)
- Modify: `src/gsuite_mcp/gdoc_ops.py` (wire snapshot)
- Modify: `src/gsuite_mcp/server.py` (env var for backup folder)

### Step 1: Write the failing test

```python
# tests/test_backup_copy.py
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

    # Verify copy was called with correct parent
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

    result = await create_backup_copy(
        service, "original_id", backup_folder_id="custom_folder",
    )

    copy_call = service.files().copy.call_args
    body = copy_call.kwargs.get("body") or copy_call[1].get("body")
    assert body["parents"] == ["custom_folder"]
```

### Step 2: Run test to verify it fails

Run: `uv run pytest tests/test_backup_copy.py -v`
Expected: FAIL with `ImportError: cannot import name 'create_backup_copy'`

### Step 3: Implement

Add to `src/gsuite_mcp/drive_ops.py`:

```python
async def create_backup_copy(
    service,
    file_id: str,
    backup_folder_id: str | None = None,
) -> dict[str, Any]:
    """Create a backup copy of a file before a destructive edit.

    Returns {backup_file_id, backup_file_name}.
    """
    from datetime import datetime, timezone

    meta = await asyncio.to_thread(
        lambda: service.files()
        .get(fileId=file_id, fields="name,parents")
        .execute()
    )
    name = meta.get("name", "Untitled")
    parents = meta.get("parents", [])
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    backup_name = f"{name}__autobackup_{timestamp}"

    target_parent = backup_folder_id or (parents[0] if parents else None)
    body: dict[str, Any] = {"name": backup_name}
    if target_parent:
        body["parents"] = [target_parent]

    copied = await asyncio.to_thread(
        lambda: service.files()
        .copy(fileId=file_id, body=body, fields="id,name")
        .execute()
    )

    return {
        "backup_file_id": copied["id"],
        "backup_file_name": copied["name"],
    }
```

### Step 4: Run tests

Run: `uv run pytest tests/test_backup_copy.py -v`
Expected: All PASS

### Step 5: Commit

Auto-committed by hook.

---

## Task 8: Wire auto-snapshot into replace_section and gdoc_batch_replace

**Files:**
- Modify: `tests/test_replace_section.py`
- Modify: `tests/test_gdoc_batch_replace.py`
- Modify: `src/gsuite_mcp/server.py`

### Step 1: Write the failing tests

Add to `tests/test_replace_section.py`:

```python
@pytest.mark.asyncio
async def test_server_replace_section_auto_snapshot_on_confirmed_blast(mock_services):
    """Confirmed blast-radius edit creates backup and returns backup_file_id."""
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
        "id": "backup_123", "name": "My Doc__autobackup_2026-06-04T12:00:00Z",
    }

    result = await server_replace_section(
        file_id="d1",
        section_heading="Chapter 1",
        new_content="y" * 200 + "\n",
        confirm_delete_chars=6653,
    )

    assert "error" not in result
    assert result["backup_file_id"] == "backup_123"
    drive.files().copy.assert_called_once()
```

### Step 2: Run test to verify it fails

Run: `uv run pytest tests/test_replace_section.py::test_server_replace_section_auto_snapshot_on_confirmed_blast -v`
Expected: FAIL — `backup_file_id` not in result

### Step 3: Implement

The auto-snapshot logic belongs in `server.py` wrappers since they have access to drive_service.

In `server.py` `replace_section` tool, after getting the result from `docs_ops.replace_section`:

```python
    # Auto-snapshot on confirmed blast-radius trips
    if (
        "error" not in result
        and not result.get("dry_run")
        and confirm_delete_chars is not None
    ):
        backup_folder = os.environ.get("BACKUP_FOLDER_ID")
        try:
            backup = await drive_ops.create_backup_copy(
                drive, file_id, backup_folder_id=backup_folder,
            )
            result["backup_file_id"] = backup["backup_file_id"]
            result["backup_file_name"] = backup["backup_file_name"]
        except Exception:
            pass  # Don't fail the edit if backup fails
```

Wait — the snapshot should happen BEFORE the edit, not after. The design says: "create a backup before executing." So the flow in the server wrapper should be:

1. Check trashed, mimeType
2. Call `docs_ops.replace_section` with `dry_run=True` to get computed values
3. If blast-radius would fire AND confirm_delete_chars is provided → create backup
4. Call `docs_ops.replace_section` for real

Actually, that's wasteful — two doc fetches. Better: let the server wrapper handle the snapshot. When `confirm_delete_chars` is provided, snapshot BEFORE calling the real replace_section. But we don't know if blast-radius will actually fire until we compute chars_deleted.

Simplest approach: in the server wrapper, if `confirm_delete_chars` is set, always create a backup before the edit. This is slightly more conservative (creates backup even for edits that wouldn't have tripped the guard), but `confirm_delete_chars` is only ever passed when the guard already fired once, so the user knows it's a big edit.

Actually, even simpler and matching the design: the `replace_section` function in docs_ops already computes everything. We can restructure the server wrapper:

1. Always call docs_ops.replace_section normally
2. If it returns BLAST_RADIUS_EXCEEDED, return that error
3. If confirm_delete_chars is passed and the edit succeeds, that means the blast-radius guard was bypassed. Create backup BEFORE by doing a dry_run first, then backup, then real call.

The cleanest approach: in server.py, when `confirm_delete_chars` is set:
1. Create backup first
2. Then call docs_ops.replace_section (which will bypass the guard since confirm_delete_chars matches)

```python
    # Auto-snapshot: when confirm_delete_chars is provided, backup first
    backup_info = {}
    if confirm_delete_chars is not None:
        backup_folder = os.environ.get("BACKUP_FOLDER_ID")
        try:
            backup = await drive_ops.create_backup_copy(
                drive, file_id, backup_folder_id=backup_folder,
            )
            backup_info = {
                "backup_file_id": backup["backup_file_id"],
                "backup_file_name": backup["backup_file_name"],
            }
        except Exception:
            pass

    result = await docs_ops.replace_section(...)
    if "error" not in result:
        result.update(backup_info)
```

Similarly for `gdoc_batch_replace` in server.py.

### Step 4: Run tests

Run: `uv run pytest tests/test_replace_section.py tests/test_gdoc_batch_replace.py -v`
Expected: All PASS

### Step 5: Commit

Auto-committed by hook.

---

## Task 9: Structured diff summary on replace_section

**Files:**
- Modify: `tests/test_replace_section.py`
- Modify: `src/gsuite_mcp/docs_ops.py`

### Step 1: Write the failing test

Add to `tests/test_replace_section.py`:

```python
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
```

### Step 2: Run test to verify it fails

Run: `uv run pytest tests/test_replace_section.py::test_replace_section_returns_section_span -v`
Expected: FAIL — `section_span` not in result (unless already added in Task 3)

### Step 3: Implement

This should already be partially done in Task 3. Ensure ALL return paths from `replace_section` include `section_span` and `anchor_is_styled_heading`. The existing return dict (line 342-349) needs these fields merged in:

```python
    return {
        "file_id": file_id,
        "section_heading": heading["text"],
        "heading_level": heading["heading_level"],
        "characters_deleted": characters_deleted,
        "characters_inserted": characters_inserted,
        "include_heading": include_heading,
        "section_span": section_span,
        **anchor_fields,
    }
```

### Step 4: Run tests

Run: `uv run pytest tests/test_replace_section.py -v`
Expected: All PASS

### Step 5: Commit

Auto-committed by hook.

---

## Task 10: Env var wiring for blast-radius thresholds

**Files:**
- Modify: `src/gsuite_mcp/server.py`
- Modify: `tests/test_replace_section.py`

### Step 1: Write the failing test

Add to `tests/test_replace_section.py`:

```python
@pytest.mark.asyncio
async def test_server_replace_section_custom_blast_threshold(mock_services):
    """BLAST_RADIUS_MIN_DELTA env var raises the threshold."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().get.return_value.execute.return_value = {
        "name": "My Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-06-04T12:00:00Z",
    }

    # 500 chars deleted, 10 inserted → normally would trip (delta=490>200, ratio=50>2)
    doc = _make_doc(
        (0, 10, "Chapter 1\n", "HEADING_1"),
        (10, 510, "x" * 499 + "\n", "NORMAL_TEXT"),
        (510, 520, "Chapter 2\n", "HEADING_1"),
    )
    docs.documents().get.return_value.execute.return_value = doc
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    import os
    with patch.dict(os.environ, {"BLAST_RADIUS_MIN_DELTA": "1000"}):
        result = await server_replace_section(
            file_id="d1",
            section_heading="Chapter 1",
            new_content="y" * 9 + "\n",
        )

    # With min_delta=1000, the 490 delta doesn't trip
    assert "error" not in result
```

### Step 2: Run test to verify it fails

Run: `uv run pytest tests/test_replace_section.py::test_server_replace_section_custom_blast_threshold -v`
Expected: FAIL — env var not being read

### Step 3: Implement

In `server.py`, read env vars and pass to docs_ops:

```python
    min_delta = int(os.environ.get("BLAST_RADIUS_MIN_DELTA", "200"))
    max_ratio = float(os.environ.get("BLAST_RADIUS_MAX_RATIO", "2"))
```

Pass these to `docs_ops.replace_section` as new kwargs `blast_min_delta` and `blast_max_ratio`, which it forwards to `check_blast_radius`.

Actually, cleaner: have the server wrapper call `check_blast_radius` itself, or pass the env values through. Since `check_blast_radius` already accepts `min_delta` and `max_ratio`, we can add those params to `replace_section` and `batch_replace`.

Simplest: add `blast_min_delta: int = 200` and `blast_max_ratio: float = 2.0` to both functions, and have server.py read the env vars and pass them.

### Step 4: Run tests

Run: `uv run pytest tests/test_replace_section.py -v`
Expected: All PASS

### Step 5: Commit

Auto-committed by hook.

---

## Task 11: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

### Step 1: Update tool descriptions

Update the `replace_section` tool description:
```
8. `replace_section` — replace content by heading/section in Google Docs. Supports `dry_run`, `expected_delete_chars` (precision check), `confirm_delete_chars` (blast-radius bypass). Flags NORMAL_TEXT anchors with `anchor_is_styled_heading` and `section_extends_to`.
```

Update the `gdoc_batch_replace` tool description:
```
18. `gdoc_batch_replace` — batch find/replace in a live Google Doc. **Breaking:** `expected_count` now defaults to 1 (pass explicit count for multi-match, or `null` to disable). Supports `confirm_delete_chars` for blast-radius bypass.
```

### Step 2: Add new env vars

```
- `BLAST_RADIUS_MIN_DELTA` — min chars difference to trigger blast-radius guard (default 200)
- `BLAST_RADIUS_MAX_RATIO` — min deletion/insertion ratio to trigger guard (default 2)
- `BACKUP_FOLDER_ID` — Drive folder for auto-snapshots (defaults to same folder as file)
```

### Step 3: Update test count

Run tests, update the count:
```
- `tests/` — pytest suite mirroring the module split (N tests)
```

### Step 4: Add breaking change note

Add under Key Constraints:
```
- **Breaking (v2):** `gdoc_batch_replace` edits now default to `expected_count: 1`. Pass the real count for multi-match edits, or `null` to disable the check.
```

### Step 5: Commit

Auto-committed by hook.

---

## Task 12: Full test suite verification

### Step 1: Run the full test suite

Run: `uv run pytest -v`
Expected: All tests PASS

### Step 2: Run linter

Run: `uv run ruff check .`
Expected: No errors

### Step 3: Verify test count and update CLAUDE.md if needed

Count tests in output, update CLAUDE.md test count to match.

---

## Summary of new tests per file

| File | New tests |
|------|-----------|
| `tests/test_blast_radius.py` | 9 |
| `tests/test_replace_section.py` | ~10 |
| `tests/test_gdoc_batch_replace.py` | ~6 |
| `tests/test_backup_copy.py` | 2 |
| **Total new** | **~27** |
