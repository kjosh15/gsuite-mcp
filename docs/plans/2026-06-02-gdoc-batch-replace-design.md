# Design: `gdoc_batch_replace` — Safe in-place batch editing

**Date:** 2026-06-02
**Status:** Approved
**PRD:** Provided by Josh (inline)

## Problem

No in-place batch editor for Google Docs. Current tools either fork to a new
`.docx` file (breaking file ID preservation), require one API call per
find/replace pair (no atomicity), or can't cross paragraph boundaries.

## Decisions

- **Approach:** Client-side batch via `deleteContentRange` + `insertText`
  (Approach A). Reuses existing `_flatten_doc_text` pattern from `replace_regex`.
- **Denylist:** Env var `GDOC_REVIEW_DOC_IDS` (comma-separated file IDs).
- **Revision IDs:** Drive `revisions.list` before and after mutation (2 extra
  API calls). These are the IDs that work for rollback in the Drive UI.
- **Matching:** Exact, case-sensitive only. No regex, no per-pair match_case.
- **Cross-paragraph:** Supported naturally via flattened text + delete/insert.

## Architecture

Three layers following existing project structure:

### 1. `docs_ops.batch_replace()`

Core logic: flatten doc, find matches, validate counts, build requests.

```python
async def batch_replace(
    docs_service,
    file_id: str,
    edits: list[dict],  # [{find_text, replace_text, expected_count?}]
    dry_run: bool = False,
) -> dict[str, Any]:
```

**Algorithm:**
1. Fetch full document via `documents.get()`
2. `_flatten_doc_text()` to get flat text + index map
3. For each pair, find all occurrences (case-sensitive substring search)
4. If any pair has `expected_count` and actual != expected → abort with
   per-pair report, no mutation
5. Check for overlapping match regions across pairs → abort if found
6. If `dry_run` → return per-pair counts only
7. Build `deleteContentRange` + `insertText` requests in reverse document
   order (all pairs interleaved by position)
8. Single `batchUpdate` call

**Output:**
```json
{
  "results": [
    {"find_text": "...", "matches_found": 1,
     "expected_count": 1, "status": "ok"}
  ],
  "committed": true,
  "total_replacements": 5
}
```

### 2. `gdoc_ops.batch_replace()` wrapper

Enriches with Drive revision IDs.

```python
async def batch_replace(
    drive_service, docs_service, file_id, edits, dry_run=False
) -> dict[str, Any]:
```

1. Fetch latest Drive revision ID before mutation
2. Call `docs_ops.batch_replace()`
3. If committed, fetch latest Drive revision ID after
4. Return enriched result with `revision_id_before` and `revision_id_after`

### 3. `server.py` tool: `gdoc_batch_replace`

```python
@mcp.tool()
async def gdoc_batch_replace(
    file_id: str,
    edits: list[dict],
    dry_run: bool = False,
    allow_review_docs: bool = False,
) -> dict[str, Any]:
```

**Guards (in order):**
1. Trashed file → `TRASHED_FILE`
2. MIME type → `NOT_A_GOOGLE_DOC`
3. Denylist → `REVIEW_DOC_BLOCKED` (if file_id in env var and flag is False)
4. Validate edits non-empty, each has find_text + replace_text
5. Delegate to `gdoc_ops.batch_replace()`
6. HttpError wrapping

## Error cases

| Scenario | Error key | Behavior |
|---|---|---|
| Count mismatch on any pair | `COUNT_MISMATCH` | Whole batch aborts, per-pair report |
| Overlapping match regions | `OVERLAPPING_MATCHES` | Abort, identifies conflicting pairs |
| Find text not found (no expected_count) | N/A | `matches_found: 0`, pair is a no-op |
| Empty edits array | `INVALID_INPUT` | Refuse |
| Review doc without flag | `REVIEW_DOC_BLOCKED` | Refuse |

## Testing

New file: `test_gdoc_batch_replace.py`

- Happy path: multi-pair batch commits
- Count mismatch aborts entire batch
- Dry run returns counts without mutation
- Cross-paragraph find/replace collapses to one paragraph
- Overlapping match detection
- Review doc denylist guard
- Trashed file refusal
- Non-Google-Doc MIME refusal
- Revision IDs present in response
- Zero-match pair treated as no-op (not an error)

## Non-goals

- Native Docs suggestion mode (yellow accept/reject UI)
- Changing `gdoc_suggest_edit` behavior
- Cross-document edits
- Regex or case-insensitive matching
