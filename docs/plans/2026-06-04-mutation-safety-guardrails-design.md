# Mutation Safety Guardrails — Design

**Date:** 2026-06-04
**Motivation:** A `replace_section` call matched a NORMAL_TEXT paragraph as a fallback heading, computed a section boundary extending to end-of-document, and deleted 6,653 characters to insert 201 — silently destroying the document tail. No guardrail fired because: (a) the tool had no dry-run, (b) the blast radius was invisible to the caller, (c) no backup existed.

## Changes

### 1. Blast-radius guard (tool-agnostic)

Before any mutation tool commits a write, compute `chars_deleted` and `chars_inserted`. If **both** conditions hold:

- `chars_deleted - chars_inserted > BLAST_RADIUS_MIN_DELTA` (env var, default **200**)
- `chars_deleted > chars_inserted * BLAST_RADIUS_MAX_RATIO` (env var, default **2**)

…then **refuse** the write unless the caller passes `confirm_delete_chars=<N>` where N equals the computed `chars_deleted`.

**Applies to:** `replace_section`, `gdoc_batch_replace`, `format_document` (delete actions).

**Pure deletions** (inserting nothing) will always trip the guard — that's intentional. The `confirm_delete_chars` path is a single integer field the caller echoes back, not a dialog box. This keeps intentional trims ergonomic: the first call returns the computed count, the second call echoes it. Two round-trips, no friction beyond proving you saw the number.

**Return on refusal:**
```json
{
  "error": "BLAST_RADIUS_EXCEEDED",
  "retryable": true,
  "chars_deleted": 6653,
  "chars_inserted": 201,
  "net_change": -6452,
  "message": "Deletion exceeds safety threshold. Pass confirm_delete_chars=6653 to proceed.",
  "section_span": {"start_index": 142, "end_index": 6795}
}
```

### 2. replace_section: dry_run + expected_delete_chars

Add two parameters:

- **`dry_run: bool = False`** — when true, returns the computed section span, `chars_deleted`, `anchor_is_styled_heading`, and `section_extends_to` without writing. Matches the pattern already established by `gdoc_batch_replace`.
- **`expected_delete_chars: int | None = None`** — when provided, the computed `chars_deleted` must match or the tool aborts with `DELETE_CHARS_MISMATCH`. This is independent of the blast-radius guard (which uses `confirm_delete_chars`); `expected_delete_chars` is a precision check, the guard is a magnitude check.

**Workflow:** Caller does `dry_run=true` first, sees the span, then calls with `expected_delete_chars` matching the returned count. If the blast-radius guard also fires, they additionally pass `confirm_delete_chars`.

### 3. replace_section: flag NORMAL_TEXT anchors

When the matched paragraph is a fallback (not HEADING_1–6), include in the response:

- `anchor_is_styled_heading: false`
- `section_extends_to: "END_OF_DOCUMENT"` (when no formal heading terminates the section)

These fields appear in both `dry_run` responses and normal responses. Combined with #1 and #2, the caller can see the danger before committing.

No behavioral change (we don't refuse NORMAL_TEXT matches) — the information is surfaced so the blast-radius guard and expected_delete_chars do the enforcement.

### 4. gdoc_batch_replace: default expected_count to 1

If `expected_count` is omitted from an edit dict, default to **1** instead of `None`.

**Migration note (breaking change):** Previously, omitting `expected_count` meant "replace all matches." Now it means "expect exactly 1 match; abort if not." Callers doing intentional multi-match edits must pass `expected_count: <N>` explicitly. This is a one-line changelog entry:

> **Breaking:** `gdoc_batch_replace` edits now default to `expected_count: 1`. Pass the real count for multi-match edits.

### 5. Auto-snapshot on blast-radius trips

When the blast-radius guard fires **and** the caller confirms (passes `confirm_delete_chars`), create a backup **before** executing:

1. `drive.files().copy()` the file to `{title}__autobackup_{ISO timestamp}` in the same folder (or `BACKUP_FOLDER_ID` env var if set).
2. Execute the mutation.
3. Return `backup_file_id` in the response.

**Only on confirmed blast-radius trips.** Small edits: no overhead, no folder clutter. The backup is a full Drive copy, so restoring is just "use this file ID."

### 6. Structured diff summary on all mutations

Every mutation tool response includes:

```json
{
  "chars_deleted": 6653,
  "chars_inserted": 201,
  "net_change": -6452
}
```

`replace_section` additionally returns `section_span: {start_index, end_index}` and `anchor_is_styled_heading: bool`.

`gdoc_batch_replace` already returns per-pair `matches_found`; add aggregate `chars_deleted` / `chars_inserted` / `net_change` at the top level.

## Implementation order

1. Blast-radius guard helper (shared function in `docs_ops.py`)
2. replace_section: dry_run, expected_delete_chars, NORMAL_TEXT flagging
3. gdoc_batch_replace: default expected_count to 1
4. Auto-snapshot (drive_ops.py helper + wiring in server.py)
5. Structured diff summary across all mutation tools
6. Tests for each change
7. Update CLAUDE.md (tool descriptions, env vars, test count)

## Env vars (new)

| Var | Default | Purpose |
|-----|---------|---------|
| `BLAST_RADIUS_MIN_DELTA` | `200` | Min chars difference to trigger guard |
| `BLAST_RADIUS_MAX_RATIO` | `2` | Min deletion/insertion ratio to trigger guard |
| `BACKUP_FOLDER_ID` | *(none)* | Drive folder for auto-snapshots (defaults to same folder as file) |

## Files touched

- `src/gsuite_mcp/docs_ops.py` — blast-radius helper, replace_section changes, batch_replace default
- `src/gsuite_mcp/gdoc_ops.py` — snapshot wiring, diff summary for batch_replace
- `src/gsuite_mcp/drive_ops.py` — `create_backup_copy()` helper
- `src/gsuite_mcp/server.py` — new params on replace_section, confirm_delete_chars on tools, env var reads
- `tests/test_replace_section.py` — dry_run, expected_delete_chars, NORMAL_TEXT flag, blast-radius refusal
- `tests/test_gdoc_batch_replace.py` — default expected_count, blast-radius on large edits
- `tests/test_blast_radius.py` — shared guard unit tests
- `CLAUDE.md` — updated tool descriptions, env vars, test count
