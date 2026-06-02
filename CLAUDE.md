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
- `src/gsuite_mcp/drive_ops.py` — Drive v3 operations (download, upload, search, metadata, comments, trash/untrash)
- `src/gsuite_mcp/docs_ops.py` — Docs v1 operations (append, replace_text, replace_section, read_paragraph_at_path)
- `src/gsuite_mcp/sheets_ops.py` — Sheets v4 operations (append rows)
- `src/gsuite_mcp/docx_edits.py` — OOXML tracked-changes (pure functions)
- `src/gsuite_mcp/gdoc_ops.py` — Google Doc operations (template populate, suggest edit via .docx export, batch replace with revision IDs)
- `src/gsuite_mcp/gmail_ops.py` — Gmail v1 operations (thread-aware draft creation)
- `src/gsuite_mcp/retry.py` — retry helper with exponential backoff for transient Google API errors (5xx, 429)
- `src/gsuite_mcp/api_key_middleware.py` — Starlette auth middleware (bearer token or `?key=` query param)
- `src/gsuite_mcp/server.py` — FastMCP server exposing 18 tools (refuses to start without `GSUITE_MCP_API_KEY`)
- `tests/` — pytest suite mirroring the module split (238 tests)
- `docs/DEPLOYMENT.md` — deployment runbook (Cloud Run topology, Secret Manager layout, key rotation, smoke tests, client config)

## Tools

1. `download_file` — download or export a file
2. `upload_file` — create or update a file (returns `bytes_uploaded` + `file_size` for truncation detection)
3. `search_files` — Drive query syntax search
4. `get_file_metadata` — single-file metadata
5. `get_files_metadata` — batch metadata for N files
6. `append_to_file` — native append for Docs/Sheets; roundtrip fallback for plain files
7. `replace_text` — exact + regex replace in Google Docs. Supports `expected_count` (pre-check before mutation), `preceded_by`/`followed_by` (context-anchored filtering within 200-char window)
8. `replace_section` — replace content by heading/section in Google Docs (heading detection + positional delete/insert)
9. `format_document` — batch paragraph formatting: set_style, set_text_style (bold/italic/underline/strikethrough), delete, delete_by_index, delete_empty_after, insert_paragraph (by index, inherits list formatting), insert_paragraph_after_match (by text match). Multi-match protection: >1 match fails unless `match_all: true`. `preview: true` for dry-run.
10. `manage_comments` — list/create/reply/resolve on Drive comments
11. `docx_suggest_edit` — tracked-change revision marks in .docx files
12. `create_reply_draft` — thread-aware Gmail draft creation (draft only, human sends)
13. `gdoc_template_populate` — copy template → native Google Doc, replace placeholders
14. `gdoc_suggest_edit` — export Google Doc as .docx, apply tracked change, re-upload as new .docx
15. `trash_file` — move a file to Drive trash (reversible within 30 days)
16. `untrash_file` — restore a trashed file from Drive trash
17. `read_paragraph_at_path` — navigate Google Doc heading/list structure via path syntax (e.g. `TASKS / Career / #2`), returns text + indices

## Environment Variables

Required:
- `GOOGLE_OAUTH_CLIENT_ID` — OAuth 2.0 client ID from GCP console
- `GOOGLE_OAUTH_CLIENT_SECRET` — OAuth 2.0 client secret
- `GOOGLE_OAUTH_REFRESH_TOKEN` — long-lived refresh token (generate via `auth_setup`)

Optional:
- `GSUITE_MCP_API_KEY` — shared secret for the bearer-token middleware (also accepts `GDRIVE_MCP_API_KEY` for backward compatibility)
- `PORT` — HTTP port for the FastMCP server (default 8080)

## Key Constraints

- No database, no state, no LLM calls
- Single-user OAuth only (service accounts removed)
- Streamable HTTP transport for Cloud Run
- `docx_suggest_edit` requires matches to fit within one paragraph (v1)
- Gmail scopes: `gmail.compose` + `gmail.readonly` (narrowest for drafts). Users must re-run `auth_setup` after upgrade to grant Gmail scopes.
- Mutation tools refuse trashed files with `error: TRASHED_FILE`. Use `untrash_file` to restore first.
- Read tools return `trashed: true` with `trashed_time` for files in Drive trash.

## Session Tracking
Total Claude sessions: 14
Last session: 2026-05-23 11:32:25
