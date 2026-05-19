# Design: Template/Styling Improvements + Trashed File Detection

**Date:** 2026-05-19
**Status:** Approved

---

## Part A: Template & Styling Improvements

### Scope

The PRD proposed 5 changes. After review, 2 already exist in `format_document` (`set_style` and `delete` actions). The net-new work is 4 changes:

### A1. Fix `replace_section` for empty sections

**File:** `src/gsuite_mcp/docs_ops.py` (lines 200-209)

**Current:** Returns `EMPTY_SECTION` error when `delete_start >= delete_end` and `include_heading=False`.

**New behavior:** When section body is empty (`delete_start >= delete_end`):
- `include_heading=False`: Skip `deleteContentRange`. Issue only `insertText` at `heading["end_index"]` + `updateParagraphStyle` on inserted range. Returns `characters_deleted: 0`.
- `include_heading=True`: Works as today (deletes heading, inserts new content, restores heading style).

**Edge case — last section with no body:** Handled naturally. `_find_section_end` returns document end, which equals `heading["end_index"]` for an empty last section, triggering the insert-only path.

### A2. Add regex match mode to `format_document`

**File:** `src/gsuite_mcp/docs_ops.py`

Extend `_find_paragraphs_matching` with a `match_mode` parameter:
- `"exact"` (default) — strip + casefold equality (current behavior)
- `"substring"` — needle-in-text (current `substring: true`)
- `"regex"` — `re.search(pattern, text)` per paragraph (no casefold; regex controls case via `(?i)`)

**Affected actions:** `set_style`, `delete`, `delete_empty_after`

**Validation:** When `match_mode == "regex"`, compile pattern in validation phase. Return `INVALID_REGEX` error on failure.

**Backward compat:** `substring: true` treated as `match_mode: "substring"`. If both set, `match_mode` wins.

### A3. `post_styles` parameter for `gdoc_template_populate`

**Files:** `src/gsuite_mcp/gdoc_ops.py`, `src/gsuite_mcp/server.py`

Add `post_styles: list[dict] | None = None` parameter. Each dict is a `format_document` operation (same schema — no new format to learn).

After `replaceAllText` batchUpdate, if `post_styles` is provided, call `format_document(docs_service, file_id, post_styles)` internally. Result includes `post_styles_result` field. Failure in post_styles does not roll back template creation.

### A4. Documentation — paragraph boundary behavior

Update `replace_text` tool description in `server.py`:

> Operates on paragraph-level text. Patterns spanning paragraph breaks will not match (Google Docs treats breaks as structural objects, not `\n`). Use `format_document` with `delete` action for paragraph-level operations.

Update `format_document` description to mention `match_mode` and regex support.

---

## Part B: Trashed File Detection and Refusal

### B1. Add `trashed` field to metadata responses

**File:** `src/gsuite_mcp/drive_ops.py`

Add `"trashed,trashedTime"` to `fields` parameter in all `files().get()` and `files().list()` calls. Map to response as `trashed: bool` and `trashed_time: str | None`.

Affected functions: `get_file_metadata`, `get_files_metadata`, `search_files`, `download_file`, `list_recent_files`.

Do NOT filter trashed files from search results. Surface them with flag set.

### B2. Refuse mutations on trashed files

**File:** `src/gsuite_mcp/server.py`

Mutation tools already fetch metadata for MIME-type checks. Extend: if `meta.get("trashed")`, return:

```python
{
    "error": "TRASHED_FILE",
    "file_id": file_id,
    "file_name": meta.get("name", ""),
    "trashed_time": meta.get("trashedTime", ""),
    "retryable": False,
    "message": "Cannot modify trashed file. Restore via Drive UI or call untrash_file first.",
}
```

**Operations in scope:** `replace_text`, `replace_section`, `append_to_file`, `format_document`, `manage_comments` (create/reply/resolve only), `gdoc_suggest_edit`, `docx_suggest_edit`, `upload_file` (update mode), `gdoc_template_populate` (not needed — creates new files).

### B3. Add `trash_file` and `untrash_file` tools

**File:** `src/gsuite_mcp/drive_ops.py` (new functions), `src/gsuite_mcp/server.py` (tool registration)

Both use `files().update(fileId=file_id, body={"trashed": True/False})`.

- `trash_file`: Returns `{file_id, trashed: true, trashed_time}`. Error if already trashed or not found.
- `untrash_file`: Returns `{file_id, trashed: false}`. Error if not trashed or not found.

### B4. Tool description updates

Add trash behavior notes to each tool's docstring:
- Read tools: "Returns `trashed: true` with `trashed_time` if the file is in Drive trash."
- Mutation tools: "Refuses trashed files with `error: TRASHED_FILE`. Use `untrash_file` to restore first."

---

## Files touched (both PRDs)

| File | Changes |
|------|---------|
| `src/gsuite_mcp/docs_ops.py` | A1 (replace_section fix), A2 (regex match mode) |
| `src/gsuite_mcp/gdoc_ops.py` | A3 (post_styles parameter) |
| `src/gsuite_mcp/drive_ops.py` | B1 (trashed field), B3 (trash/untrash ops) |
| `src/gsuite_mcp/server.py` | A3 (post_styles wiring), A4 (docs), B2 (trash refusal), B3 (tool registration), B4 (descriptions) |
| `tests/test_replace_section.py` | A1 tests |
| `tests/test_format_document.py` | A2 tests |
| `tests/test_gdoc_ops.py` | A3 tests |
| `tests/test_drive_ops.py` | B1, B3 tests |
| `tests/test_server.py` or inline | B2 tests |
| `CLAUDE.md` | Update tool count and descriptions |
