# format_document Enhancements: Insert Paragraphs & Text Styles

**Date:** 2026-05-22

## Summary

Add three new actions to the existing `format_document` batch operation: `insert_paragraph`, `insert_paragraph_after_match`, and `set_text_style`. These enable programmatic list editing and inline text formatting within the same atomic batch call.

## New Actions

### `insert_paragraph`

Insert a new paragraph after a content block index.

```json
{
    "action": "insert_paragraph",
    "after_paragraph_index": 3,
    "text": "New item text",
    "list_id": null,
    "nesting_level": null,
    "text_style": {"italic": true}
}
```

- `after_paragraph_index` (required): content block index, same convention as `delete_by_index`
- `text` (required): paragraph content; `\n` appended if missing
- `list_id` (optional): override; default inherits from neighbor paragraph's `bullet.listId`
- `nesting_level` (optional): override; default inherits from neighbor paragraph's `bullet.nestingLevel`
- `text_style` (optional): dict of `{bold, italic, underline, strikethrough}` — only specified keys are changed
- Out-of-range index returns `index_out_of_range` status

### `insert_paragraph_after_match`

Find a paragraph by text, insert a new paragraph after it.

```json
{
    "action": "insert_paragraph_after_match",
    "find_text": "Hulda to explore...",
    "text": "New follow-up item",
    "inherit_list_formatting": true,
    "nesting_level": null,
    "list_id": null,
    "text_style": {"italic": true}
}
```

- Uses `_find_paragraphs_matching` with existing `match_mode`/`substring` options
- Multi-match → `multi_match_error` (no `match_all` support — inserting after all matches is rarely intended)
- `inherit_list_formatting` (default `false`): when `true`, reads list_id + nesting_level from matched paragraph
- `list_id`/`nesting_level` overrides work the same as `insert_paragraph`

### `set_text_style`

Apply inline text styles to matched paragraphs.

```json
{
    "action": "set_text_style",
    "find_text": "some phrase",
    "style": {"italic": true, "strikethrough": false}
}
```

- Matches at paragraph level using `_find_paragraphs_matching`
- Multi-match protection with `match_all` opt-in (same as `set_style`/`delete`)
- `style` dict: any subset of `{bold, italic, underline, strikethrough}`
- Only specified keys are changed (dynamic `fields` mask); omitted keys left unchanged
- Validation: must have at least one recognized key, otherwise `INVALID_TEXT_STYLE` error

## Text Style Validation

Shared by all three actions. Valid keys: `bold`, `italic`, `underline`, `strikethrough`. All values must be boolean. At least one key required when `text_style`/`style` is provided.

## Implementation Details

### Index Management

Inserts use the existing `pending` list keyed by `startIndex`. The descending-sort before `batchUpdate` ensures higher-index operations execute first, preventing index shifting.

### List Inheritance

Google Docs auto-inherits list formatting when inserting at a list paragraph's `endIndex`. Explicit API calls only needed when overriding `nesting_level` (via `updateParagraphStyle` with bullet nesting) or when the neighbor isn't a list but `list_id` is explicitly provided.

### Sub-request Order Per Insert

1. `insertText` — creates the paragraph
2. `updateTextStyle` — applies bold/italic/etc. on the inserted range
3. `updateParagraphStyle` — adjusts nesting if override provided

### Files Modified

- `src/gsuite_mcp/docs_ops.py` — add three new action handlers in `format_document()`
- `src/gsuite_mcp/server.py` — update docstring for `format_document` tool
- `tests/test_format_document.py` — tests for all three actions
- `CLAUDE.md` — update tool #9 description
