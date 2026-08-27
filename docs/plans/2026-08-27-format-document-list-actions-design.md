# format_document List Actions: set_list & clear_list

**Date:** 2026-08-27

## Summary

Add two actions to `format_document`: `set_list` (turn matched paragraphs into
list items) and `clear_list` (remove bullets). Before this, `format_document`
could set named paragraph styles (`NORMAL_TEXT`, `TITLE`, `SUBTITLE`,
`HEADING_1..6`) but had no way to make a paragraph a list item — so a document
produced by `gdoc_template_populate` or `gdoc_batch_replace` came out with every
bullet as a plain paragraph and had to be finished by hand in the Docs UI.

Because `gdoc_template_populate`'s `post_styles` argument is forwarded verbatim
to `format_document` as its `operations`, both actions work there with no
additional code.

## New Actions

### `set_list`

```json
{
    "action": "set_list",
    "find_text": "First item",
    "preset": "BULLET_DISC_CIRCLE_SQUARE",
    "nesting_level": 0
}
```

- Implemented with `documents.batchUpdate` `createParagraphBullets`.
- `preset` (optional, default `BULLET_DISC_CIRCLE_SQUARE`): any Docs API
  `BulletGlyphPreset`, exported as `docs_ops.VALID_BULLET_PRESETS` (15 values).
  `NUMBERED_DECIMAL_ALPHA_ROMAN` is the ordered-list counterpart. An
  unrecognized value returns `INVALID_PRESET` during validation, before any
  document read or write.
- `nesting_level` (optional, default 0): non-negative integer, else
  `INVALID_NESTING_LEVEL`.
- Matching uses `_find_paragraphs_matching`, so `find_text` / `substring` /
  `match_mode` behave exactly as they do for `set_style`.
- Multi-match → `multi_match_error` (same shape as `set_style`: `matches` list
  of `{paragraph_index, text}`), unless `"match_all": true`.

### `clear_list`

```json
{"action": "clear_list", "find_text": "First item"}
```

- Implemented with `deleteParagraphBullets`.
- Takes no `preset`. `nesting_level` is meaningless here and is deliberately
  **not** echoed back in the result, so a result never reports a value that was
  not applied.
- Same matching, multi-match, range-form and preview rules as `set_list`.

### Range form (both actions)

```json
{
    "action": "set_list",
    "from_text": "First item",
    "to_text": "Last item",
    "preset": "BULLET_DISC_CIRCLE_SQUARE"
}
```

Bullets every paragraph from the `from_text` match through the `to_text` match,
**inclusive**, as a single `createParagraphBullets` spanning the whole run.

This is the common case — four consecutive lines under a heading. Without it the
caller needs one operation per line, or a `match_all` substring that happens to
hit exactly the right paragraphs and nothing else.

Endpoint resolution rules:

| Condition | Result |
|---|---|
| Either endpoint missing or blank | `MISSING_RANGE_TEXT` (validation, no API call) |
| An endpoint matches 0 paragraphs | `not_found`, with `find_text` = the missing endpoint and `endpoint` = `"from_text"`/`"to_text"` |
| An endpoint matches >1 paragraph | `multi_match_error`, same shape as `set_style`, plus `endpoint` naming the ambiguous side |
| `to_text` resolves before `from_text` | `invalid_range`, with `from_paragraph_index` and `to_paragraph_index` |

`match_all` is not consulted for the range form — each endpoint must be
unambiguous by definition.

## Nesting

The Docs API has no nesting parameter on `createParagraphBullets`; list depth is
driven by paragraph indentation. A non-zero `nesting_level` therefore emits a
second `updateParagraphStyle` request setting `indentStart` and
`indentFirstLine` to `nesting_level * 36pt` — the same 36pt-per-level convention
`insert_paragraph` already uses (`_NESTING_INDENT_PT`).

`nesting_level: 0` emits **no** extra request, leaving the default bullet indents
Docs applies itself. Emitting a 0pt indent instead would flatten them.

## Result Shape

`set_list` and `clear_list` emit exactly one result entry per operation
(not one per match), carrying `paragraph_indices` — every paragraph the
operation touches — in both preview and applied results:

```json
{
    "action": "set_list",
    "from_text": "First item",
    "to_text": "Last item",
    "preset": "BULLET_DISC_CIRCLE_SQUARE",
    "paragraph_indices": [1, 2, 3, 4],
    "status": "applied"
}
```

Under `preview: true`, `status` is `would_apply` and no `batchUpdate` is issued.

## Implementation Details

### Index Management

Both actions append to the existing `pending` list keyed by `startIndex`, and
ship in the same single `batchUpdate` as every other operation in the call, so
ordering stays predictable. The pre-existing descending sort by `startIndex`
means each request executes before any earlier-in-the-document request that
could shift its indices — including the leading tabs `createParagraphBullets`
strips from a paragraph it bullets.

Python's `list.sort` is stable, so requests generated for the same `startIndex`
keep the order they were produced in: bullets are created first, then the
nesting indent is applied to the same range.

### Sub-request Order Per set_list

1. `createParagraphBullets` — converts the paragraph(s) to list items
2. `updateParagraphStyle` — indent for `nesting_level`, only when non-zero

### Range-form Span

One request spanning `from_block["startIndex"]` → `to_block["endIndex"]`, rather
than one request per paragraph. This matches how the Docs API is meant to be
driven and keeps the request count constant regardless of run length. Raw block
indices are used (as `set_style` does), not the `_clamp_delete_end` clamp the
deletion paths need.

`paragraph_indices` skips any non-paragraph block (e.g. a table) that falls
inside the span, so the reported indices are only the paragraphs.

### Files Modified

- `src/gsuite_mcp/docs_ops.py` — `VALID_BULLET_PRESETS`, `DEFAULT_BULLET_PRESET`,
  `_NESTING_INDENT_PT`, `_is_range_op()`, `_bullet_requests()`, validation and
  two handlers in `format_document()`
- `src/gsuite_mcp/server.py` — `format_document` tool docstring; and
  `gdoc_template_populate`'s docstring, to state outright that `post_styles`
  takes the same operation schema as `format_document`'s `operations` (the
  previous "same schema as format_document" wording did not make that clear)
- `src/gsuite_mcp/gdoc_ops.py` — `template_populate` docstring, same point
- `tests/test_format_document.py` — 29 tests across both actions
- `tests/test_gdoc_template_populate.py` — range-form `set_list` driven through
  `post_styles`, asserting the bullet request reaches `batchUpdate`
- `CLAUDE.md` — tool #12 and #16 descriptions

## Testing

Built test-first. 29 new tests in `tests/test_format_document.py` covering: the
preset default and the ordered preset, `INVALID_PRESET`, missing `find_text`,
`not_found`, multi-match and `match_all`, nesting indent emitted / not emitted at
level 0, `INVALID_NESTING_LEVEL`, preview for both forms, the inclusive range
span, single-paragraph range, `MISSING_RANGE_TEXT` on either side, endpoint
`not_found`, reversed range, ambiguous endpoint, range + nesting, all four
`clear_list` paths, and one test asserting `set_list` shares a single
`batchUpdate` with `set_style` in back-to-front order.

Full suite: 510 passed, 2 skipped. `ruff check .` clean.

## Out of Scope

- Per-paragraph nesting within one range operation (the whole span gets one
  level). Callers needing mixed depth issue one operation per level.
- `bulletPreset` values are passed through as given; no attempt is made to map
  friendly names ("bullet", "numbered") onto presets.
- Reading current bullet state back — `read_document` and
  `read_paragraph_at_path` already expose `nesting_level` for list paragraphs.
