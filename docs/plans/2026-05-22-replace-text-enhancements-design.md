# Replace Text Enhancements Design

Date: 2026-05-22

## Problem

Text replacement in Google Docs fails silently or destructively when:
1. A find string matches more (or fewer) times than expected — no way to assert count before mutation
2. The same string appears in multiple contexts — no way to target "the one near X"
3. Bullet/list items can only be addressed by text match, not structural position — fragile when text is duplicated

## Feature 1: `expected_count` on `replace_text`

### API Change

Add `expected_count: Optional[int] = None` to `replace_text` tool.

### Behavior

When `expected_count` is set:
1. Fetch full document via `documents.get`
2. Count occurrences client-side (substring match for exact mode, regex for regex mode)
3. If count != expected_count, return error `COUNT_MISMATCH` with `{expected, actual}` — no mutation
4. If count matches, proceed with replacement (replaceAllText for exact, batched delete+insert for regex)

When `expected_count` is None (default): existing behavior unchanged — no extra API call.

### Error Response

```json
{
  "error": "COUNT_MISMATCH",
  "retryable": false,
  "expected_count": 1,
  "actual_count": 3,
  "message": "Expected 1 occurrence(s) but found 3. No changes made."
}
```

## Feature 2: `preceded_by` / `followed_by` context anchors

### API Change

Add to `replace_text`:
- `preceded_by: Optional[str] = None`
- `followed_by: Optional[str] = None`

### Behavior

When either context param is set, switch from `replaceAllText` to client-side approach:
1. Fetch full document, build flattened text + index map (same pattern as `replace_regex`)
2. Find all occurrences of `find` in flattened text
3. For each match, check within 200 chars before/after for context strings (case-sensitive if match_case=True)
4. Filter to only context-matching occurrences
5. Build reverse-order delete+insert requests, execute as single batchUpdate

Context anchors compose with:
- `regex=True` — find matches via regex, then filter by context
- `expected_count` — checked after context filtering, before mutation

### 200-char window

Measured in flattened document text (crosses paragraph boundaries). Context strings are matched as substrings within the window.

## Feature 3: `read_paragraph_at_path` tool

### API

New tool: `read_paragraph_at_path(file_id, path, include_children=False)`

### Path Syntax

Segments delimited by ` / ` (space-slash-space). Each segment resolves against siblings at its nesting level:
1. **Text prefix match** (case-insensitive, stripped) — preferred
2. **Positional: `#N`** (1-based index among siblings) — fallback when segment starts with `#`

Example: `TASKS / Career / Careers that allow` or `TASKS / Career / #2`

### Tree Model

1. Headings (HEADING_1..6) define top-level nodes. Heading rank determines nesting: HEADING_1 is root, HEADING_2 is child of nearest preceding HEADING_1, etc.
2. Within a section, paragraphs with `bullet.nestingLevel` define list hierarchy. Level N is child of nearest preceding level N-1.
3. Non-heading, non-bullet paragraphs are children of the current section at nesting level 0.

### Return Structure

```json
{
  "file_id": "...",
  "path": "TASKS / Career / Careers that allow",
  "text": "Careers that allow us to live in Iceland",
  "paragraph_index": 42,
  "nesting_level": 2,
  "start_index": 1234,
  "end_index": 1280
}
```

With `include_children=True`, adds:
```json
{
  "children": [
    {"text": "Teaching English", "nesting_level": 3, "paragraph_index": 43},
    {"text": "Software engineering", "nesting_level": 3, "paragraph_index": 44}
  ]
}
```

### Error Cases

- `PATH_NOT_FOUND` — no match at some segment
- `AMBIGUOUS_PATH_SEGMENT` — multiple prefix matches at one level (returns candidates)
- `NOT_A_GOOGLE_DOC` — file isn't a Google Doc

## Implementation Order

1. `expected_count` — smallest change, highest value, touches only `docs_ops.py` + `server.py`
2. `preceded_by`/`followed_by` — builds on the client-side doc-read pattern, extends `replace_text`
3. `read_paragraph_at_path` — new tool, new tree-building logic, most complex
