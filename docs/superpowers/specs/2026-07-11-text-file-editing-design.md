# Design: Server-side editing for non-Google text files

**Date:** 2026-07-11
**Status:** Approved design, pending implementation plan
**Origin:** PRD "Server-side editing for non-Google text files in gsuite-mcp" (2026-07-10), refined via brainstorming session.

## 1. Problem

`gsuite-mcp` can edit native Google Docs surgically (`replace_text`, `gdoc_batch_replace`, `replace_section`) with guardrails: `expected_count`, `dry_run`, blast-radius checks, revision IDs for rollback. It has no equivalent for plain-text files in Drive (`.md`, `.txt`, `.csv`, `.json`, `.yaml`). The only write paths today are `append_to_file` (append-only) and `upload_file` (full-file replacement, requiring the caller to materialize the entire file as a base64 literal in a tool call).

For an LLM caller with a bounded context window, full-file replacement of anything beyond a few KB is unreliable — the payload risks silent truncation, producing a silently corrupted document. This is not hypothetical: on 2026-07-09/10, an attempt to apply seven verified edits to a 15.7KB markdown strategy doc (`Key_Strategy_Zanzibar.md`) failed because the base64 payload could not be safely reconstructed in context, and the fallback probe (`append_to_file`) left a stray comment in the live file. See PRD Appendix for full detail.

## 2. Goal

Give plain-text Drive files the same class of surgical, guarded, in-place editing that native Google Docs already have, without ever requiring the caller to transmit full file contents.

## 3. Non-goals

- Binary formats (`.docx`, `.xlsx`, `.pdf`) — covered by existing tools.
- Rich-text/formatting operations — these are plain-text files.
- Replacing `upload_file` for genuine file creation or wholesale replacement.
- Merge/conflict resolution across concurrent editors (see §7 concurrency — we detect and refuse, we don't merge).
- `text_replace_section` (markdown-heading-keyed replace) — deferred; heading detection is a parsing problem worth deferring until usage on the plain `text_replace`/`text_batch_replace` tools shows demand.
- `expected_count` range semantics (`{min, max}`) — deferred to a later phase if needed.

## 4. Scope of this build

All three tools ship together in this pass (no phase split):

- `text_replace`
- `text_batch_replace`
- `text_read_range`

The blast-radius guard and autobackup are included from the start (not deferred), given these tools operate on the same class of strategy documents the Google Docs guard already protects. An optimistic-concurrency check is included from the start, given a concrete observed risk: two Claude sessions edited the same file within hours of each other on 2026-07-09.

## 5. Architecture

Follows the existing per-domain-module convention (`docs_ops.py`, `gdoc_ops.py`, `sheets_ops.py`):

- **New `src/gsuite_mcp/text_ops.py`** — pure functions, no Drive/network calls:
  - `is_supported_mime(mime_type: str) -> bool` — allowlist: any `text/*` prefix (covers `text/plain`, `text/markdown`, `text/csv`, `text/yaml`), plus the exact types `application/json` and `application/x-yaml`. Everything else, including all `application/vnd.google-apps.*` types and generic `application/octet-stream`, is refused.
  - `decode_text(raw: bytes) -> dict` — strict UTF-8 decode; returns `{"text": str, "line_ending": "\n" | "\r\n" | "mixed"}`, or signals `NOT_TEXT_FILE` on `UnicodeDecodeError`. No lossy fallback codec, ever.
  - `encode_text(text: str, line_ending: str) -> bytes` — re-encodes UTF-8, no BOM, restoring the original line-ending convention.
  - `count_matches(content: str, find: str, match_case: bool, regex: bool) -> int`
  - `apply_replace(content: str, find: str, replace: str, match_case: bool, regex: bool) -> str`
  - `apply_batch(content: str, edits: list[dict]) -> dict` — applies edits sequentially against the in-memory buffer (edit *n* sees the result of edits *1..n-1*, same contract as `gdoc_batch_replace`). All-or-nothing: on the first edit whose `expected_count` doesn't match, aborts and reports which edit (index + `find` preview) failed, with no partial application.

- **`drive_ops.py` addition**: `download_file_bytes(service, file_id) -> bytes` — thin wrapper around `files().get_media()`. `append_to_file`'s plain-roundtrip branch in `server.py` (currently an inline raw `get_media` call) switches to this, so download logic isn't duplicated across two code paths. The write side already goes through `drive_ops.upload_file()` in both places — no change needed there. This refactor does **not** change `append_to_file`'s semantics: it still operates on raw bytes (no UTF-8 strictness), preserving its ability to append to any file type it currently supports.

- **Reused as-is, no changes**:
  - `docs_ops.check_blast_radius()` — already generic (char-count based), not Docs-specific.
  - `drive_ops.create_backup_copy()` — autobackup-on-confirm pattern.
  - `pagination.py`'s opaque cursor codec — used by `text_read_range` instead of inventing new pagination logic.

- **`server.py`** — three new `@mcp.tool()` wrappers, following the existing docstring/error-dict conventions (plain dicts with `error`/`retryable`/`message`, no custom exception classes).

## 6. Tool signatures

### `text_replace`

```
text_replace(
  file_id: str,
  find: str,
  replace: str,
  expected_count: int | None = None,
  match_case: bool = True,
  regex: bool = False,
  dry_run: bool = False,
  confirm_delete_chars: int | None = None,
) -> {
  file_id, file_name, mime_type,
  matches_found: int,
  bytes_before: int, bytes_after: int,
  chars_deleted: int, chars_inserted: int, net_change: int,
  revision_id_before: str, revision_id_after: str | None,
  modified_time: str,
  backup_file_id: str | None,
}
```

Matching operates on the raw text stream (newlines are ordinary characters — multi-line and cross-paragraph `find` patterns work naturally; there's no paragraph object model to work around, unlike Google Docs).

### `text_batch_replace`

```
text_batch_replace(
  file_id: str,
  edits: [{find: str, replace: str, expected_count: int | None}],
  dry_run: bool = False,
  confirm_delete_chars: int | None = None,
) -> {
  ...(same aggregate fields as text_replace),
  per_edit: [{index: int, find_preview: str, matches_found: int, applied: bool}],
}
```

One download, one upload, regardless of edit count.

### `text_read_range`

```
text_read_range(
  file_id: str,
  start_line: int | None = None,
  end_line: int | None = None,
  max_bytes: int = 100_000,
  cursor: str | None = None,
) -> {content: str, total_lines: int, truncated: bool, next_cursor: str | None, mime_type, line_ending}
```

Read-only. Lets a caller pull a bounded slice to construct a `find` string without downloading the whole file.

## 7. Mutation flow (`text_replace` / `text_batch_replace`)

Every step below returns before any write on failure — no partial state is ever left on disk.

1. Fetch metadata (`name, mimeType, modifiedTime, md5Checksum, trashed, trashedTime, size`) → `TRASHED_FILE` if trashed.
2. MIME allowlist check → `UNSUPPORTED_MIME`. If the MIME is a Google Doc/Sheet type, the error message points to `replace_text`/`gdoc_batch_replace` instead.
3. Size ceiling (5MB) → `FILE_TOO_LARGE`.
4. Download bytes (`drive_ops.download_file_bytes`), strict UTF-8 decode (`text_ops.decode_text`) → `NOT_TEXT_FILE` on failure. Detect line ending to preserve on write.
5. Snapshot `modifiedTime` + `md5Checksum` from step 1 as the concurrency baseline.
6. Compute match count(s) via `text_ops.count_matches` / `apply_batch` → `COUNT_MISMATCH` (reports actual count), `NO_MATCH`, or `BATCH_ABORTED` (names the failing edit index). Nothing written yet — safe even without `dry_run`.
7. If `dry_run=True`: return here with `matches_found` / `per_edit` and no write.
8. Blast-radius check (`docs_ops.check_blast_radius`, threshold `BLAST_RADIUS_MIN_DELTA`/`BLAST_RADIUS_MAX_RATIO` env vars, same as `replace_section`) → `BLAST_RADIUS_EXCEEDED` unless `confirm_delete_chars` matches the computed deletion. On a confirmed trip, `drive_ops.create_backup_copy()` runs first and `backup_file_id` is returned.
9. **Concurrency check**: re-fetch `modifiedTime`/`md5Checksum` immediately before upload, compare to the step-5 snapshot → `CONCURRENT_MODIFICATION` if changed, write nothing. This is the mitigation for the observed parallel-session collision risk.
10. Re-encode (`text_ops.encode_text`) with the original line ending, upload via `drive_ops.upload_file()` (existing truncation detection via `bytes_uploaded`/`file_size` applies unchanged).
11. Revision IDs: see §8 — behavior here depends on an implementation-time spike.
12. Return the result dict.

## 8. Open technical risk: revision ID semantics (resolve at implementation start)

The existing `gdoc_batch_replace` pattern fetches `revision_id_before`/`after` via `revisions().list()` and treats the head revision ID as a reliable undo point. This is well-established for Google Docs. It is **not confirmed** to work the same way for plain-text Drive files — Drive's content-revision retention for non-Google-native files depends on revision retention settings, and the head revision ID's semantics there haven't been verified against a real file.

**Resolution plan**: the first implementation task is a spike — call `revisions().list()` on a real `.md` test file in Drive before/after a `files().update()` media upload, and confirm whether a usable revision ID comes back.

- **If revisions are reliable**: implement exactly as `gdoc_batch_replace` does — return `revision_id_before`/`after`, document them as a real rollback path. Backup copies remain gated on blast-radius confirmation only (per §7 step 8), matching the existing Google Docs guard's cost profile.
- **If revisions are not reliable**: `drive_ops.create_backup_copy()` runs unconditionally before every write in step 8 (not just on a confirmed blast-radius trip), and `revision_id_before`/`after` are still returned but documented as best-effort/informational only, not a guaranteed rollback path. `backup_file_id` becomes the documented rollback mechanism in this branch, always present on a successful mutation.

This conditional is resolved once, during implementation, not re-decided per call.

## 9. Error taxonomy

| Code | Meaning | Mutation occurred? |
|---|---|---|
| `TRASHED_FILE` | File is in trash | No |
| `UNSUPPORTED_MIME` | MIME outside allowlist | No |
| `FILE_TOO_LARGE` | Exceeds 5MB ceiling | No |
| `NOT_TEXT_FILE` | Not decodable as UTF-8 | No |
| `COUNT_MISMATCH` | `expected_count` didn't match actual | No |
| `NO_MATCH` | `find` string absent | No |
| `BATCH_ABORTED` | One edit in a batch failed its `expected_count` | No |
| `BLAST_RADIUS_EXCEEDED` | Deletion exceeds guard, no/mismatched confirmation | No |
| `CONCURRENT_MODIFICATION` | File changed between read and write | No |

Every code above is a no-write path. There is no partial-write error state — `BATCH_ABORTED` in particular must leave the file byte-identical to before the call.

## 10. Testing

TDD per project convention (write failing test first):

- `tests/test_text_ops.py` — pure-function unit tests: line-ending detection/preservation, `apply_batch` sequential/all-or-nothing behavior, MIME allowlist edge cases (Google Doc MIME rejected with the right pointer, binary MIME rejected).
- `tests/test_text_replace.py` — mocked Drive service (style matches `tests/test_append.py`'s `mock_services` fixture): dry_run, expected_count match/mismatch, blast-radius trip + confirm + backup, concurrency collision, trashed refusal, non-UTF-8 refusal.
- `tests/test_text_batch_replace.py` — multi-edit atomicity, one-of-N failure leaves file untouched and names the failing edit, sequential-edit-sees-prior-edit semantics.
- `tests/test_text_read_range.py` — pagination cursor round-trip, `max_bytes` truncation, line-range bounds.
- Existing `tests/test_append.py` gets a small addition/adjustment to cover the `download_file_bytes` refactor without behavior change.

## 11. Acceptance criteria (carried from PRD, unchanged)

1. A 20KB `.md` in Drive can receive seven distinct surgical edits in one `text_batch_replace` call, without the caller transmitting file contents.
2. A `find` string spanning multiple lines matches correctly.
3. `expected_count=1` on a string occurring twice aborts with `COUNT_MISMATCH` and leaves the file byte-identical.
4. `dry_run=True` on a valid batch returns per-edit match counts and writes nothing.
5. A batch where edit 4 of 7 fails leaves the file byte-identical, and names edit 4 in the error.
6. A file containing CRLF line endings retains CRLF after an edit that touches neither.
7. `revision_id_before` from a successful call can be used to restore the prior version — **if** the §8 spike confirms this is possible; otherwise `backup_file_id` serves this role and criterion 7 is re-validated as "a backup copy from a successful call can restore the prior version."
8. Calling `text_replace` on a native Google Doc returns a clear pointer to `replace_text` rather than an opaque failure.
9. Two concurrent edits to the same file: the second writer gets `CONCURRENT_MODIFICATION`, not a silent overwrite.
