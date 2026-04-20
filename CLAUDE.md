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
- `src/gsuite_mcp/drive_ops.py` — Drive v3 operations (download, upload, search, metadata, comments)
- `src/gsuite_mcp/docs_ops.py` — Docs v1 operations (append, replace_text)
- `src/gsuite_mcp/sheets_ops.py` — Sheets v4 operations (append rows)
- `src/gsuite_mcp/docx_edits.py` — OOXML tracked-changes (pure functions)
- `src/gsuite_mcp/gmail_ops.py` — Gmail v1 operations (thread-aware draft creation)
- `src/gsuite_mcp/api_key_middleware.py` — Starlette auth middleware (bearer token or `?key=` query param)
- `src/gsuite_mcp/server.py` — FastMCP server exposing 10 tools (refuses to start without `GSUITE_MCP_API_KEY`)
- `tests/` — pytest suite mirroring the module split (54 tests)
- `docs/DEPLOYMENT.md` — deployment runbook (Cloud Run topology, Secret Manager layout, key rotation, smoke tests, client config)

## Tools

1. `download_file` — download or export a file
2. `upload_file` — create or update a file
3. `search_files` — Drive query syntax search
4. `get_file_metadata` — single-file metadata
5. `get_files_metadata` — batch metadata for N files
6. `append_to_file` — native append for Docs/Sheets; roundtrip fallback for plain files
7. `replace_text` — exact + regex replace in Google Docs
8. `manage_comments` — list/create/reply/resolve on Drive comments
9. `docx_suggest_edit` — tracked-change revision marks in .docx files

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

## Session Tracking
Total Claude sessions: 4
Last session: 2026-04-20
