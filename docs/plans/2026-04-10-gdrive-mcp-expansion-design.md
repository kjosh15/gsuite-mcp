# gdrive-mcp Expansion Design

**Date:** 2026-04-10
**Status:** Approved, ready for implementation plan

## Context

User feedback identified five gaps in the current gdrive-mcp server:

1. **No native append** — every append requires a download-decode-concat-encode-upload cycle, which is fragile and context-expensive
2. **Google Docs round-tripping loses formatting** — uploading text/plain back to a Google Doc flattens headers, bold, tables
3. **Tracked changes are not first-class** — no equivalent of the "old docx_tool suggest_edit"
4. **OAuth scope gap** — service-account auth can't access Gemini meeting notes and other user-owned content
5. **Metadata requires per-file downloads** — no lightweight batch freshness scan

## Goals

Close all five gaps with a minimal increase in tool count. Stay under the practical MCP tool budget (~10 tools). Keep the single-user deployment model simple. Preserve test coverage.

## Non-goals

- Multi-user / multi-tenant support
- Real-time Drive push notifications / webhooks (`files.watch`)
- Cross-paragraph matches in `docx_suggest_edit` (v2)
- Fuzzy / semantic matching in `replace_text` (v2)

## Design decisions

### 1. Auth model: replace service account with OAuth user credentials

**Why:** Every gap (#2, #3, #4 specifically) traces back to the service-account auth model's limitations:
- Service accounts have no Drive storage quota, so personal-Drive `create` always fails
- Service accounts can't see user-owned Gemini notes
- Service accounts can't set documents into Suggesting mode

Since this server is single-user, there is no benefit to keeping service-account auth as a fallback. Removing it deletes code, tests, and a whole branch of error handling (`storageQuotaExceeded`).

**Implementation:**
- New module `auth.py` (renamed from `drive.py`)
- Reads `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN` from env
- Constructs `google.oauth2.credentials.Credentials`, refreshes on first use, caches at module level
- Scopes: `drive`, `documents`, `spreadsheets` (all full-access user scopes)
- New `auth_setup.py` CLI: `python -m gdrive_mcp.auth_setup` runs `InstalledAppFlow.run_local_server()` and prints the refresh token
- New dep: `google-auth-oauthlib>=1.2.0`
- `GOOGLE_SERVICE_ACCOUNT_JSON` env var is removed entirely
- The `storageQuotaExceeded` branch in `upload_file` is deleted (dead code under OAuth)

### 2. Code structure: split by API surface

```
src/gdrive_mcp/
├── __init__.py
├── __main__.py          (unchanged)
├── auth.py              (NEW — OAuth credential loading, service factories)
├── auth_setup.py        (NEW — CLI for OAuth consent flow)
├── drive_ops.py         (NEW — download, upload, search, metadata, comments)
├── docs_ops.py          (NEW — append_text_to_doc, replace_all_text)
├── sheets_ops.py        (NEW — append_rows)
├── docx_edits.py        (NEW — OOXML tracked-changes, pure function)
└── server.py            (thin FastMCP wrapper; imports from *_ops modules)
```

Each `*_ops.py` module exposes plain async functions that take a service object and args. `server.py` is a thin layer of `@mcp.tool()` decorators that wire auth + ops together. Tests mirror the module split.

### 3. Tool surface: 9 MCP tools

**Original 4 (unchanged except as noted):**

1. `download_file(file_id, export_format=None)` — unchanged
2. `upload_file(content_base64, file_name, mime_type, file_id=None, parent_folder_id=None)` — `storageQuotaExceeded` branch removed
3. `search_files(query, max_results=10)` — unchanged
4. `get_file_metadata(file_id)` — unchanged

**New 5:**

5. **`get_files_metadata(file_ids: list[str])`** — batch freshness scan
   - Fires N concurrent `files.get()` calls via `asyncio.gather(..., return_exceptions=True)`
   - Returns `{results: [...], errors: [{file_id, error}]}`
   - Partial failures don't abort the whole request

6. **`append_to_file(file_id, content, separator="\n")`** — polymorphic on mime type
   - Google Docs → Docs API `batchUpdate` with `InsertTextRequest` at `endOfSegmentLocation`
   - Google Sheets → Sheets API `spreadsheets.values.append` with `USER_ENTERED`
   - Anything else → download-concat-upload fallback
   - Returns `{file_id, file_name, mime_type, bytes_appended, modified_time, mode}` where `mode ∈ {"docs_native", "sheets_native", "plain_roundtrip"}`

7. **`replace_text(file_id, find, replace, match_case=True, regex=False)`** — Google Docs only
   - `regex=False` → Docs API `ReplaceAllTextRequest` with `matchCase`, one call
   - `regex=True` → client-side: `documents.get`, `re.finditer` to locate matches, build a single `batchUpdate` with paired `DeleteContentRangeRequest` + `InsertTextRequest`
   - Returns structured error if file is not a Google Doc (points to `docx_suggest_edit`)
   - Returns `{file_id, replacements_made, regex_mode, modified_time}`

8. **`manage_comments(file_id, action, comment_id=None, content=None, anchor_text=None, include_resolved=False)`** — consolidated CRUD
   - `action="list"` → `comments.list`, returns threads with replies
   - `action="create"` → `comments.create` (optionally anchored to `anchor_text`)
   - `action="reply"` → `comments.replies.create` (requires `comment_id`, `content`)
   - `action="resolve"` → `comments.update` with `resolved=true` (requires `comment_id`)
   - Structured errors for missing required params per action

9. **`docx_suggest_edit(file_id, find_text, replace_text, author="Claude")`** — real .docx files only
   - Download bytes → `docx_edits.insert_tracked_change(bytes, find, replace, author)` → upload bytes
   - `docx_edits` module manipulates OOXML directly: parses `word/document.xml`, walks `<w:p>`/`<w:r>` trees, locates `find_text` across runs, splits runs at match boundaries, wraps deleted content in `<w:del>` + `<w:delText>`, inserts `<w:ins>` with replacement inheriting the first run's `<w:rPr>`
   - Supports multi-run matches within a single paragraph
   - Errors on: Google Docs file, `find_text` not found, match spans paragraph boundary

**Removed from consideration:** per-tool `add_comment`, `list_comments`, `reply_to_comment`, `resolve_comment` — folded into `manage_comments` to stay under the tool-count budget.

### 4. Error handling philosophy (unchanged from existing code)

- **User-facing errors** (file not found, not a Google Doc, `find_text` missing, invalid regex, wrong action params) → structured error dict `{error, retryable: false, message}` so LLMs don't retry pointlessly
- **Unexpected errors** (HTTP 500, network failures) → raise, let FastMCP surface them

### 5. Testing plan (TDD)

Each new tool follows RED → GREEN → refactor.

**New test files:**
- `tests/test_auth.py` — credential loading, refresh path, missing-env errors (rewritten from test_drive.py)
- `tests/test_append.py` — all three paths (docs_native, sheets_native, plain_roundtrip)
- `tests/test_replace_text.py` — happy path, regex path, not-a-doc error, zero-match, invalid regex
- `tests/test_manage_comments.py` — all four actions + missing-param errors
- `tests/test_docx_edits.py` — pure-function tests on real .docx bytes fixture: single-run match, 2-run match, 3-run match, match at run boundaries, not-found, cross-paragraph error
- `tests/test_docx_suggest_edit.py` — integration: mocks download/upload, verifies docx_edits called correctly
- `tests/test_get_files_metadata.py` — N concurrent gets, partial-failure case

**Updated test files:**
- `tests/test_upload.py` — `storageQuotaExceeded` test deleted; create + update happy paths kept
- `tests/test_download.py`, `tests/test_search.py`, `tests/test_metadata.py` — unchanged

**Test fixture:**
- `tests/fixtures/sample.docx` — a small real .docx with prose (including bold/italic runs) created once and committed. Used as input for `test_docx_edits.py`.

### 6. Dependencies

**Added:**
- `google-auth-oauthlib>=1.2.0` (for `auth_setup.py` CLI only)

**Not added:**
- `python-docx` — doesn't support revision marks. `docx_edits.py` uses `zipfile` + `xml.etree.ElementTree` (stdlib only).

### 7. Documentation updates

- `CLAUDE.md` — replace `GOOGLE_SERVICE_ACCOUNT_JSON` references with the three OAuth env vars; document `python -m gdrive_mcp.auth_setup`
- `pyproject.toml` description — update to reflect expanded capability ("Google Drive, Docs, Sheets MCP server with tracked-changes and append support")

## Alternatives considered

### Auth model: keep service account as fallback
Rejected. Single-user deployment makes the fallback dead weight. Removing it deletes code, tests, and the `storageQuotaExceeded` branch.

### suggest_edit via Google Docs native suggestions
Rejected. The Google Docs API does not let owners create suggestions programmatically. Would require a second-account/commenter-role setup that adds deployment complexity for no single-user benefit. `replace_text` + `manage_comments(action="create")` covers the "edit + audit trail" use case; `docx_suggest_edit` covers true tracked changes for real .docx files.

### Extend `get_file_metadata` to accept a list
Rejected. Polymorphic input/output shapes are confusing for LLMs and violate one-tool-one-job. Separate `get_files_metadata` is clearer.

### Drive HTTP batch API for `get_files_metadata`
Rejected. `BatchHttpRequest` is sync-only and callback-based; wrapping it in `asyncio.to_thread` defeats the latency benefit. `asyncio.gather` is simpler and fast enough for ~10-file scans.

### Four separate comment tools
Rejected. 12 total tools exceeds the comfortable MCP budget; the comment tools are a natural CRUD cluster that consolidates cleanly into a single action-dispatched tool.

## Out of scope (explicit)

- Drive push notifications (`files.watch`) — high complexity, unclear benefit, requires stateful server
- Cross-paragraph matches in `docx_suggest_edit` — v2; workaround is multiple per-paragraph calls
- Fuzzy / semantic matching in `replace_text` — v2; exact + regex covers known needs
- Multi-user / multi-tenant auth — single-user tool

## Success criteria

- All 9 tools pass unit tests with mocked Drive/Docs/Sheets services
- `docx_edits.py` pure-function tests pass on a real .docx fixture
- `ruff check .` clean
- `CLAUDE.md` updated with new env vars and setup instructions
- Existing behavior preserved for the 4 original tools (minus the dead quota branch)
