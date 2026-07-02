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
- `src/gsuite_mcp/drive_ops.py` — Drive v3 operations (download, upload, search, metadata, comments, trash/untrash, backup copy)
- `src/gsuite_mcp/docs_ops.py` — Docs v1 operations (append, replace_text, replace_section, read_paragraph_at_path, read_document_body)
- `src/gsuite_mcp/sheets_ops.py` — Sheets v4 operations (append rows)
- `src/gsuite_mcp/docx_edits.py` — OOXML tracked-changes (pure functions)
- `src/gsuite_mcp/gdoc_ops.py` — Google Doc operations (template populate, suggest edit via .docx export, batch replace with revision IDs)
- `src/gsuite_mcp/gmail_ops.py` — Gmail v1 operations (thread-aware draft creation, inbox delivery via messages.insert, read_thread)
- `src/gsuite_mcp/pagination.py` — opaque cursor codec + byte-budget windowing (pure functions)
- `src/gsuite_mcp/gmail_quotes.py` — quoted-history stripping + html-to-text (pure functions)
- `src/gsuite_mcp/retry.py` — retry helper with exponential backoff for transient Google API errors (5xx, 429)
- `src/gsuite_mcp/api_key_middleware.py` — Starlette auth middleware (bearer token or `?key=` query param)
- `src/gsuite_mcp/server.py` — FastMCP server exposing 21 tools (refuses to start without `GSUITE_MCP_API_KEY`)
- `tests/` — pytest suite mirroring the module split (337 tests)
- `docs/DEPLOYMENT.md` — deployment runbook (Cloud Run topology, Secret Manager layout, key rotation, smoke tests, client config)

## Tools

1. `download_file` — download or export a file
2. `upload_file` — create or update a file (returns `bytes_uploaded` + `file_size` for truncation detection)
3. `search_files` — Drive query syntax search
4. `get_file_metadata` — single-file metadata
5. `get_files_metadata` — batch metadata for N files
6. `append_to_file` — native append for Docs/Sheets; roundtrip fallback for plain files
7. `replace_text` — exact + regex replace in Google Docs. Supports `expected_count` (pre-check before mutation), `preceded_by`/`followed_by` (context-anchored filtering within 200-char window)
8. `replace_section` — replace content by heading/section in Google Docs (heading detection + positional delete/insert). Supports `dry_run` (returns section span without writing), `expected_delete_chars` (precision check), `confirm_delete_chars` (bypass blast-radius guard). Returns `chars_deleted`, `chars_inserted`, `net_change`, `section_span`, `anchor_is_styled_heading`. NORMAL_TEXT fallback anchors flagged with `anchor_is_styled_heading: false`.
9. `format_document` — batch paragraph formatting: set_style, set_text_style (bold/italic/underline/strikethrough), delete, delete_by_index, delete_empty_after, insert_paragraph (by index, inherits list formatting), insert_paragraph_after_match (by text match). Multi-match protection: >1 match fails unless `match_all: true`. `preview: true` for dry-run.
10. `manage_comments` — list/create/reply/resolve on Drive comments
11. `docx_suggest_edit` — tracked-change revision marks in .docx files
12. `create_reply_draft` — thread-aware Gmail draft creation (draft only, human sends)
13. `gdoc_template_populate` — copy template → native Google Doc, replace placeholders
14. `gdoc_suggest_edit` — export Google Doc as .docx, apply tracked change, re-upload as new .docx
15. `trash_file` — move a file to Drive trash (reversible within 30 days)
16. `untrash_file` — restore a trashed file from Drive trash
17. `read_paragraph_at_path` — navigate Google Doc heading/list structure via path syntax (e.g. `TASKS / Career / #2`), returns text + indices
18. `gdoc_batch_replace` — batch find/replace in a live Google Doc
19. `deliver_to_inbox` — insert a message into the authenticated user's own Gmail inbox via `messages.insert` (NOT send). From/To hard-coded to `josh@josh.is`. Inputs: `subject`, `body`, `content_type`. Cannot email third parties. (in-place, atomic, cross-paragraph, count verification, dry-run, review-doc denylist). Edits default to `expected_count: 1` (breaking: pass explicit count for multi-match). Supports `confirm_delete_chars` for blast-radius guard bypass. Returns aggregate `chars_deleted`, `chars_inserted`, `net_change`.
20. `read_thread` — bounded Gmail thread read: strip_quoted_history, message_limit/cursor pagination, never-silent truncation (truncated + next_cursor), thread_changed flag on append.
21. `read_document` — bounded Google Doc read: fields projection (["body"]/["comments"]/both), structural-element pagination, STALE_CURSOR on mid-pagination edits.

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

- No database, no state, no LLM calls
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
- `read_document` comment projection fetches the first Drive comments page (pageSize 100) and sets an explicit `comments_truncated: true` when more exist; following the comment page token (full comment pagination) is out of scope for v1.

## Session Tracking
Total Claude sessions: 29
Last session: 2026-07-02 09:21:13
