# CLAUDE.md

## Commands

```bash
# Install
uv sync --all-extras

# Test
uv run pytest -q

# Lint
uv run ruff check .

# Run locally
uv run python -m gsuite_mcp

# One-time OAuth setup (generates GOOGLE_OAUTH_REFRESH_TOKEN)
uv run python -m gsuite_mcp.auth_setup
```

## Project Structure

- `src/gsuite_mcp/auth.py` — OAuth user credential loader + service factories
- `src/gsuite_mcp/auth_setup.py` — one-time OAuth consent CLI
- `src/gsuite_mcp/drive_ops.py` — Drive v3 operations (download, upload, search, metadata, comments, trash/untrash, backup copy, rename/move via update_file_metadata)
- `src/gsuite_mcp/docs_ops.py` — Docs v1 operations (append, replace_text, replace_section, read_paragraph_at_path, read_document_body)
- `src/gsuite_mcp/sheets_ops.py` — Sheets v4 operations (append rows)
- `src/gsuite_mcp/docx_edits.py` — OOXML tracked-changes (pure functions)
- `src/gsuite_mcp/gdoc_ops.py` — Google Doc operations (template populate, suggest edit via .docx export, batch replace with revision IDs)
- `src/gsuite_mcp/gmail_ops.py` — Gmail v1 operations (thread-aware draft creation, inbox delivery via messages.insert, read_thread)
- `src/gsuite_mcp/pagination.py` — opaque cursor codec + byte-budget windowing (pure functions)
- `src/gsuite_mcp/upload_session.py` — in-process chunked-upload session state (upload_id -> temp file), a scoped exception to the "no state" constraint; 30-min TTL, never persisted
- `src/gsuite_mcp/text_ops.py` — plain-text Drive file editing (matching, UTF-8/line-ending handling, guarded read-match-write core shared by text_replace/text_batch_replace, bounded read_range)
- `src/gsuite_mcp/gmail_quotes.py` — quoted-history stripping + html-to-text (pure functions)
- `src/gsuite_mcp/retry.py` — retry helper with exponential backoff for transient Google API errors (5xx, 429)
- `src/gsuite_mcp/api_key_middleware.py` — Starlette auth middleware (bearer token or `?key=` query param); 404s OAuth discovery probes (`/.well-known/oauth-*`, `openid-configuration`) *before* the auth check so MCP clients don't mistake it for an OAuth server
- `src/gsuite_mcp/server.py` — FastMCP server exposing 28 tools (refuses to start without `GSUITE_MCP_API_KEY`)
- `tests/` — pytest suite mirroring the module split (431 tests)
- `docs/DEPLOYMENT.md` — deployment runbook (Cloud Run topology, Secret Manager layout, key rotation, smoke tests, client config)

## Tools

1. `download_file` — download or export a file
2. `upload_file` — create or update a file (returns `bytes_uploaded` + `file_size` for truncation detection). Also accepts `expected_bytes`/`expected_sha256` to reject a truncated/corrupted content_base64 payload before writing.
3. `upload_file_start` — start a chunked upload session, declaring `total_bytes` (+ optional `expected_sha256`). Returns a unique `upload_id`.
4. `upload_file_chunk` — upload a single chunk with sequential `chunk_index` starting at 0. Returns `chunk_index` and `bytes_received` so far.
5. `upload_file_finish` — assemble, verify, and write the uploaded chunks. Returns the final file metadata. Sessions are in-process only, 30-min TTL, `UPLOAD_NOT_FOUND` on loss (never silent).
6. `search_files` — Drive query syntax search. Zero-result responses carry `status`: `results` | `empty` | `unresolved` (a `'<id>' in parents` clause referencing a nonexistent folder). Known Drive API limitation: `name contains 'X'` won't match a mid-word substring — documented in the tool description with the `name = 'exact'` workaround.
7. `get_file_metadata` — single-file metadata. For native Google formats (Docs, Sheets, Slides), `size_bytes` is `null` with `size_unavailable: true` instead of Drive's misleading storage-quota number. Exposes `md5_checksum` when Drive provides one.
8. `get_files_metadata` — batch metadata for N files
9. `append_to_file` — native append for Docs/Sheets; roundtrip fallback for plain files. Returns `revision_id_before`/`revision_id_after` for Docs/Sheets (monotonic, unlike `modified_time`, which the tool already re-reads post-write but which can still lag under Drive API eventual consistency). Also accepts `expected_bytes`/`expected_sha256` to reject a corrupted payload before writing.
10. `replace_text` — exact + regex replace in Google Docs. Supports `expected_count` (pre-check before mutation), `preceded_by`/`followed_by` (context-anchored filtering within 200-char window)
11. `replace_section` — replace content by heading/section in Google Docs (heading detection + positional delete/insert). Supports `dry_run` (returns section span without writing), `expected_delete_chars` (precision check), `confirm_delete_chars` (bypass blast-radius guard). Returns `chars_deleted`, `chars_inserted`, `net_change`, `section_span`, `anchor_is_styled_heading`. NORMAL_TEXT fallback anchors flagged with `anchor_is_styled_heading: false`.
12. `format_document` — batch paragraph formatting: set_style, set_text_style (bold/italic/underline/strikethrough), delete, delete_by_index, delete_empty_after, insert_paragraph (by index, inherits list formatting), insert_paragraph_after_match (by text match). Multi-match protection: >1 match fails unless `match_all: true`. `preview: true` for dry-run.
13. `manage_comments` — list/create/reply/resolve on Drive comments
14. `docx_suggest_edit` — tracked-change revision marks in .docx files
15. `create_reply_draft` — thread-aware Gmail draft creation (draft only, human sends)
16. `gdoc_template_populate` — copy template → native Google Doc, replace placeholders
17. `gdoc_suggest_edit` — export Google Doc as .docx, apply tracked change, re-upload as new .docx
18. `trash_file` — move a file to Drive trash (reversible within 30 days)
19. `untrash_file` — restore a trashed file from Drive trash
20. `read_paragraph_at_path` — navigate Google Doc heading/list structure via path syntax (e.g. `TASKS / Career / #2`), returns text + indices
21. `gdoc_batch_replace` — batch find/replace in a live Google Doc. Each edit accepts either `{find_text, replace_text}` or `{find, replace}` (text_batch_replace's key names) as an alias.
22. `deliver_to_inbox` — insert a message into the authenticated user's own Gmail inbox via `messages.insert` (NOT send). From/To hard-coded to `josh@josh.is`. Inputs: `subject`, `body`, `content_type`. Cannot email third parties. (in-place, atomic, cross-paragraph, count verification, dry-run, review-doc denylist). Edits default to `expected_count: 1` (breaking: pass explicit count for multi-match). Supports `confirm_delete_chars` for blast-radius guard bypass. Returns aggregate `chars_deleted`, `chars_inserted`, `net_change`.
23. `read_thread` — bounded Gmail thread read: strip_quoted_history, message_limit/cursor pagination, never-silent truncation (truncated + next_cursor), thread_changed flag on append.
24. `read_document` — bounded Google Doc read: fields projection (["body"]/["comments"]/both), structural-element pagination, STALE_CURSOR on mid-pagination edits.
25. `text_replace` — surgical find/replace in a plain-text Drive file (.md/.txt/.csv/.json/.yaml), server-side roundtrip (no base64 payload from caller). `expected_count` checked pre-write, `dry_run`, blast-radius guard + autobackup, optimistic-concurrency check, CRLF-preserving.
26. `text_batch_replace` — atomic multi-edit version of `text_replace`: one download/upload for N sequential find/replace pairs (edit N sees edit N-1's result), all-or-nothing on any `expected_count` mismatch. Each edit accepts either `{find, replace}` or `{find_text, replace_text}` (gdoc_batch_replace's key names) as an alias.
27. `text_read_range` — bounded line-range read of a plain-text Drive file, for building `text_replace`/`text_batch_replace` find strings without downloading the whole file.
28. `update_file_metadata` — rename a file and/or move it between folders via a single Drive `files.update` call, preserving its file ID (unlike copy+trash). Guardrails: `NO_CHANGES_REQUESTED` if all three optional args are `None` (no API call made); `TRASHED_FILE`; `CANNOT_RENAME`/`CANNOT_MOVE` when `capabilities.canRename`/`capabilities.canMoveItemWithinDrive` is false; `NOT_A_PARENT` when `remove_parent_id` isn't currently a parent (returns actual `parents`). Returns `previous_name`/`previous_parents` for verification/reversal.

## Environment Variables

Required:
- `GOOGLE_OAUTH_CLIENT_ID` — OAuth 2.0 client ID from GCP console
- `GOOGLE_OAUTH_CLIENT_SECRET` — OAuth 2.0 client secret
- `GOOGLE_OAUTH_REFRESH_TOKEN` — long-lived refresh token (generate via `auth_setup`)

Required for `gdoc_batch_replace`:
- `GDOC_REVIEW_DOC_IDS` — comma-separated file IDs for hand-review docs. `gdoc_batch_replace` **fails-closed** if this is unset or empty (returns `DENYLIST_NOT_CONFIGURED`). Docs in the list are refused unless `allow_review_docs=True`.

Optional:
- `GSUITE_MCP_API_KEY` — shared secret for the bearer-token middleware (also accepts `GDRIVE_MCP_API_KEY` for backward compatibility)
- `PORT` — HTTP port for the FastMCP server (default 8080)
- `BLAST_RADIUS_MIN_DELTA` — min chars difference to trigger blast-radius guard (default 200)
- `BLAST_RADIUS_MAX_RATIO` — min deletion/insertion ratio to trigger blast-radius guard (default 2)
- `BACKUP_FOLDER_ID` — Drive folder for auto-snapshots on confirmed blast-radius trips (defaults to same folder as file)

## Key Constraints

- Auth is API-key only (bearer or `?key=`). The middleware returns **404** for OAuth discovery paths (`/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`, `/.well-known/openid-configuration`) *before* the key check — a `401` there makes claude.ai attempt OAuth Dynamic Client Registration, which fails ("Couldn't register with the sign-in service") and repeatedly drops the connector. The 404 lets the client fall back to the `?key=` URL.
- No database, no state, no LLM calls
- `upload_file_start`/`upload_file_chunk`/`upload_file_finish` are a deliberate, scoped exception to "no database, no state": chunked-upload sessions live only in process memory plus a per-session temp file, with a 30-minute TTL. They do not survive a Cloud Run instance restart or a scale event that routes a later call to a different instance — that surfaces as a loud `UPLOAD_NOT_FOUND`, never silent data loss. Added because the server has no shared filesystem with any caller (confirmed HTTP-only transport, see `server.py:main()`), so a `source_path`-style parameter cannot work here; large `content_base64` payloads must instead be split across multiple tool calls.
- `search_threads`-style thread search and Gmail mailbox defaults are **out of scope for this repo** — this codebase has no `search_threads` tool and no Gmail-search-with-mailbox-default tool. Those behaviors belong to the separate built-in Gmail connector, not `gsuite-mcp`.
- Single-user OAuth only (service accounts removed)
- Streamable HTTP transport for Cloud Run
- `docx_suggest_edit` requires matches to fit within one paragraph (v1)
- Gmail scopes: `gmail.compose` + `gmail.readonly` + `gmail.insert` (narrowest for drafts + inbox delivery). Users must re-run `auth_setup` after upgrade to grant new scopes.
- Mutation tools refuse trashed files with `error: TRASHED_FILE`. Use `untrash_file` to restore first.
- Read tools return `trashed: true` with `trashed_time` for files in Drive trash.
- `gdoc_batch_replace` uses client-side matching with `deleteContentRange`+`insertText` (not `replaceAllText`), so cross-paragraph find/replace works. Always case-sensitive. Pure deletions (`replace_text=""`) emit only `deleteContentRange` (no empty `insertText`).
- `gdoc_batch_replace` requires `GDOC_REVIEW_DOC_IDS` to be set — fails-closed with `DENYLIST_NOT_CONFIGURED` if unset or empty. This is the safety guarantee for in-place edits: hand-review docs are walled off by the denylist.
- **Blast-radius guard** on `replace_section` and `gdoc_batch_replace`: large deletions (delta > `BLAST_RADIUS_MIN_DELTA` AND ratio > `BLAST_RADIUS_MAX_RATIO`) are refused with `BLAST_RADIUS_EXCEEDED`. Pass `confirm_delete_chars=<N>` to proceed. Confirmed blast-radius trips auto-create a backup copy before executing (returned as `backup_file_id`).
- `read_thread`/`read_document` never truncate silently — every response carries `truncated` + `next_cursor`; Docs pagination returns `STALE_CURSOR` when the doc changes mid-read, Gmail sets `thread_changed` and continues (threads are append-only).
- `read_document` comment projection fetches the first Drive comments page (pageSize 100) and sets an explicit `comments_truncated: true` when more exist; following the comment page token (full comment pagination) is out of scope for v1. Comments are returned on the first read page only (`cursor is None`); later body pages omit them (the list doesn't change across pages). A `cursor` passed to a comments-only request (`fields=["comments"]`) returns `INVALID_CURSOR` — cursors paginate the body only.
- `read_thread` decodes/quote-strips only the messages on the returned page (lazy walk from the cursor offset), so a full paginated walk is O(N) total, not O(N²).
- `text_replace`/`text_batch_replace`/`text_read_range` operate only on `text/*`, `application/json`, and `application/x-yaml` MIME types — refuse `application/vnd.google-apps.*` with a pointer to `replace_text`/`gdoc_batch_replace`, and refuse any other MIME with `UNSUPPORTED_MIME`. 5MB size ceiling (`FILE_TOO_LARGE` above that). Non-UTF-8 content is refused with `NOT_TEXT_FILE` — no lossy fallback codec.
- `text_replace`/`text_batch_replace` share one core (`text_ops.apply_edits_to_file`) with the Google Docs tools' safety guarantees: `expected_count` checked before any write, blast-radius guard (same env vars as `replace_section`/`gdoc_batch_replace`) with autobackup on a confirmed trip, and an optimistic-concurrency check (`CONCURRENT_MODIFICATION`) comparing `modifiedTime`/`md5Checksum` at read-time vs. immediately before write.
- `text_ops.ALWAYS_BACKUP_ON_WRITE` (currently `True`) makes every `text_replace`/`text_batch_replace` mutation snapshot an autobackup copy before writing, not just confirmed blast-radius trips — pending confirmation that Drive's `revisions()` API gives a reliable rollback point for plain-text files the way it does for Google Docs (see `docs/superpowers/specs/2026-07-11-text-file-editing-design.md` §8).
- `text_ops.detect_line_ending` returns `"\n"`, `"\r\n"`, or `"mixed"` (the last for files that don't use one convention uniformly, e.g. a bare `\n` alongside `\r\n`, or any lone `\r`). Only `"\n"`/`"\r\n"` files get their line endings normalized-then-restored around an edit; `"mixed"` files are decoded/encoded byte-for-byte unmodified so lines an edit didn't touch are never silently rewritten. `text_read_range`'s `line_ending` field can return `"mixed"`.
- `text_replace`/`text_batch_replace`'s `match_case=False` matching always goes through `re.IGNORECASE` (never a separately-casefolded string), so match offsets are always computed directly against the original file content — avoids corruption on inputs where casefolding changes length (e.g. German `ß`→`ss`).
- `text_read_range`'s `next_cursor` carries the original `end_line` bound (as `hard_end`) when one was given, so paginating through a budget-truncated bounded read via `next_cursor` stops at the caller's requested `end_line` rather than continuing to end-of-file.

## Session Tracking
Total Claude sessions: 64
Last session: 2026-07-23 10:16:57
