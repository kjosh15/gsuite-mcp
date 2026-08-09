# gsuite-mcp Fix Spec Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the confirmed-real defects from the 2026-08-05 gsuite-mcp fix spec (D1, D2, D4, D5, D6/D10, D8, D9) against the actual codebase, after verifying each claim against source rather than trusting the spec's prose (it was written without repo access).

**Architecture:** Each fix is a small, independent change to an existing `*_ops.py` module and/or its `server.py` tool wrapper. D9 (chunked upload) is the only new subsystem — a small in-process session registry plus three new MCP tools — needed because large `content_base64` tool-call payloads can arrive truncated/corrupted, and the server has no way to read a client's local filesystem (confirmed: HTTP-only transport, no shared filesystem with any caller, ever).

**Tech Stack:** Python 3, FastMCP, googleapiclient (Drive/Docs/Sheets/Gmail v1/v3/v4), pytest + pytest-asyncio + unittest.mock.

## Global Constraints

- Test command: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest -q`. Lint: `uv run ruff check .`.
- "No database, no state" (CLAUDE.md Key Constraints) — D9's chunked-upload session registry is a deliberate, scoped exception: in-process memory + per-session temp file only, 30-minute TTL, never persisted, and it fails loudly (`UPLOAD_NOT_FOUND`) rather than silently on loss. Document this exception explicitly in code and in CLAUDE.md.
- The server is HTTP-only — `server.py:main()` calls `mcp.http_app()` + `uvicorn.run()`, no stdio transport exists. Confirmed via `~/.claude.json`: the configured `gdrive-mcp` connection is `"type": "http"` against the deployed Cloud Run URL. No deployment mode gives the server access to a caller's local filesystem. (This is why D9 is a chunked-upload feature, not a `source_path` parameter — see the plan's cover note below.)
- D4 keeps each batch tool's existing key names canonical (`find`/`replace` for `text_batch_replace`, `find_text`/`replace_text` for `gdoc_batch_replace`) and adds acceptance of the other tool's names as an alias — no renaming, nothing breaks.
- D3 (`search_threads` truncation) and D7 (single-mailbox default) are **out of scope**: grepped `gmail_ops.py` and `server.py` and confirmed this repo has no `search_threads` tool and no Gmail-search-with-mailbox-default tool. Those behaviors belong to the separate built-in Gmail connector (`mcp__claude_ai_Gmail__search_threads`), not this codebase. Task 9 adds a one-line note to CLAUDE.md saying so; no code task exists for them.
- D2's original claim (stale pre-write `modified_time`) is **not a bug**: `append_to_file` already re-fetches `modifiedTime` post-write for Docs/Sheets (server.py, existing code). Any observed staleness is Drive API eventual consistency. Do not chase the timestamp — only add `revision_id_before`/`revision_id_after` (monotonic, unlike a timestamp), scoped to Docs/Sheets only.
- Existing safety guarantees (blast-radius guard via `BLAST_RADIUS_MIN_DELTA`/`BLAST_RADIUS_MAX_RATIO`/`BACKUP_FOLDER_ID`, `TRASHED_FILE` refusal, `GDOC_REVIEW_DOC_IDS` denylist, optimistic concurrency via `modifiedTime`/`md5Checksum`) are untouched by every task in this plan.
- Follow existing test conventions exactly: `MagicMock()` service objects, `svc.files().get.return_value.execute.return_value = {...}` for single-call mocks, a local `call_count` closure or `execute.side_effect = [...]` for multi-call mocks (see `tests/test_apply_edits_to_file.py:23-66` for the `revisions().list()`-called-twice pattern), `@pytest.mark.asyncio` on every test.

---

### Task 1: D4 — accept aliased find/replace keys on both batch tools

**Files:**
- Modify: `src/gsuite_mcp/server.py` (`gdoc_batch_replace` ~line 727, `text_batch_replace` ~line 944)
- Test: `tests/test_batch_replace_key_aliases.py` (create)

**Interfaces:**
- Produces: `_normalize_batch_edit(edit: dict[str, Any], find_key: str, replace_key: str, alias_find_key: str, alias_replace_key: str) -> dict[str, Any]` — module-level helper in `server.py`, used by both tools. No other task depends on this.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for D4: gdoc_batch_replace and text_batch_replace accept each other's key names."""

from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def mock_drive():
    with patch("gsuite_mcp.auth.get_drive_service") as mock:
        service = MagicMock()
        mock.return_value = service
        yield service


@pytest.fixture
def mock_docs():
    with patch("gsuite_mcp.auth.get_docs_service") as mock:
        service = MagicMock()
        mock.return_value = service
        yield service


@pytest.mark.asyncio
async def test_gdoc_batch_replace_accepts_find_replace_alias(mock_drive, mock_docs, monkeypatch):
    monkeypatch.setenv("GDOC_REVIEW_DOC_IDS", "some_other_doc")
    mock_drive.files().get.return_value.execute.return_value = {
        "name": "Doc", "mimeType": "application/vnd.google-apps.document",
        "trashed": False,
    }
    mock_drive.revisions().list.return_value.execute.return_value = {"revisions": []}
    mock_docs.documents().get.return_value.execute.return_value = {
        "body": {"content": [{"endIndex": 1, "paragraph": {"elements": [
            {"startIndex": 0, "endIndex": 1, "textRun": {"content": "Hello\n"}}
        ]}}]}
    }
    mock_docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    from gsuite_mcp.server import gdoc_batch_replace
    result = await gdoc_batch_replace(
        file_id="doc1",
        edits=[{"find": "Hello", "replace": "Hi"}],  # text_batch_replace's key names
        dry_run=True,
    )
    assert "error" not in result


@pytest.mark.asyncio
async def test_text_batch_replace_accepts_find_text_replace_text_alias(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = {
        "name": "notes.md", "mimeType": "text/markdown", "size": "20",
        "modifiedTime": "2026-07-10T00:00:00Z", "md5Checksum": "abc123",
        "trashed": False,
    }
    mock_drive.files().get_media.return_value.execute.return_value = b"Hello world"

    from gsuite_mcp.server import text_batch_replace
    result = await text_batch_replace(
        file_id="f1",
        edits=[{"find_text": "Hello", "replace_text": "Hi"}],  # gdoc_batch_replace's key names
        dry_run=True,
    )
    assert "error" not in result
    assert result["matches_found"] == 1


@pytest.mark.asyncio
async def test_text_batch_replace_still_rejects_missing_keys(mock_drive):
    from gsuite_mcp.server import text_batch_replace
    result = await text_batch_replace(file_id="f1", edits=[{"nope": "x"}])
    assert result["error"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_gdoc_batch_replace_still_rejects_missing_keys(mock_drive, monkeypatch):
    monkeypatch.setenv("GDOC_REVIEW_DOC_IDS", "some_other_doc")
    mock_drive.files().get.return_value.execute.return_value = {
        "name": "Doc", "mimeType": "application/vnd.google-apps.document",
        "trashed": False,
    }
    from gsuite_mcp.server import gdoc_batch_replace
    result = await gdoc_batch_replace(file_id="doc1", edits=[{"nope": "x"}])
    assert result["error"] == "INVALID_INPUT"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_batch_replace_key_aliases.py -v`
Expected: the two alias tests FAIL with `INVALID_INPUT` (current code doesn't recognize the swapped keys); the two "still rejects" tests PASS already (that's fine, they lock in existing behavior).

- [ ] **Step 3: Add the normalization helper and wire it into both tools**

In `src/gsuite_mcp/server.py`, add near the top-level helpers (after `_trashed_error`, ~line 38):

```python
def _normalize_batch_edit(
    edit: dict[str, Any],
    find_key: str,
    replace_key: str,
    alias_find_key: str,
    alias_replace_key: str,
) -> dict[str, Any]:
    """Map an edit dict's alias find/replace keys onto the canonical pair.

    Lets text_batch_replace accept gdoc_batch_replace's find_text/replace_text
    keys and vice versa, without requiring both pairs to be present at once.
    If neither pair is fully present, returns edit unchanged so the existing
    per-tool validation reports the missing-field error.
    """
    if find_key in edit and replace_key in edit:
        return edit
    if alias_find_key in edit and alias_replace_key in edit:
        normalized = dict(edit)
        normalized[find_key] = normalized.pop(alias_find_key)
        normalized[replace_key] = normalized.pop(alias_replace_key)
        return normalized
    return edit
```

In `gdoc_batch_replace`, immediately after the `if not edits:` empty-check block (~line 804) and before the `for i, edit in enumerate(edits):` validation loop:

```python
    edits = [
        _normalize_batch_edit(e, "find_text", "replace_text", "find", "replace")
        for e in edits
    ]
```

In `text_batch_replace`, immediately after its own `if not edits:` empty-check block (~line 982) and before its `for i, edit in enumerate(edits):` validation loop:

```python
    edits = [
        _normalize_batch_edit(e, "find", "replace", "find_text", "replace_text")
        for e in edits
    ]
```

Update both tools' docstrings' "Each edit:" line to note the alias, e.g. for `gdoc_batch_replace`:
`Each edit: {find_text: str, replace_text: str, expected_count?: int}. Also accepts find/replace as aliases (text_batch_replace's key names).`
and for `text_batch_replace`:
`Each edit: {find: str, replace: str, expected_count?: int, match_case?: bool, regex?: bool}. Also accepts find_text/replace_text as aliases (gdoc_batch_replace's key names).`

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_batch_replace_key_aliases.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Run full suite and commit**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest -q`
Expected: all tests pass (no regressions).

```bash
cd ~/Desktop/CODING/gsuite-mcp
git add src/gsuite_mcp/server.py tests/test_batch_replace_key_aliases.py
git commit -m "feat: accept aliased find/replace keys on both batch-replace tools (D4)"
```

---

### Task 2: D1 — native Google formats return size_unavailable, not a wrong number; expose md5_checksum

**Files:**
- Modify: `src/gsuite_mcp/drive_ops.py` (`get_file_metadata`, ~line 140)
- Test: `tests/test_drive_ops.py` (extend)

**Interfaces:**
- Produces: `get_file_metadata` now returns `size_bytes: int | None`, `size_unavailable: bool` (only `True` for native Google formats), and `md5_checksum: str` (only present when Drive provides one — i.e. never for native Docs/Sheets). No other task depends on these fields.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_drive_ops.py`, in the `get_file_metadata` section:

```python
@pytest.mark.asyncio
async def test_get_file_metadata_native_doc_size_unavailable():
    svc = _mock_drive_service({
        "id": "f1", "name": "Decision_Log",
        "mimeType": "application/vnd.google-apps.document",
        "size": "64707",  # Drive reports a number here, but it's storage
                           # quota usage, not the exported byte count — wrong.
        "modifiedTime": "2026-05-19T00:00:00Z",
        "webViewLink": "https://...", "parents": [], "capabilities": {},
    })
    result = await drive_ops.get_file_metadata(svc, "f1")
    assert result["size_bytes"] is None
    assert result["size_unavailable"] is True


@pytest.mark.asyncio
async def test_get_file_metadata_plain_file_size_accurate():
    svc = _mock_drive_service({
        "id": "f1", "name": "notes.md", "mimeType": "text/markdown",
        "size": "32285",
        "modifiedTime": "2026-05-19T00:00:00Z",
        "webViewLink": "https://...", "parents": [], "capabilities": {},
    })
    result = await drive_ops.get_file_metadata(svc, "f1")
    assert result["size_bytes"] == 32285
    assert "size_unavailable" not in result


@pytest.mark.asyncio
async def test_get_file_metadata_exposes_md5_checksum_when_present():
    svc = _mock_drive_service({
        "id": "f1", "name": "notes.md", "mimeType": "text/markdown",
        "size": "20", "md5Checksum": "d41d8cd98f00b204e9800998ecf8427e",
        "modifiedTime": "2026-05-19T00:00:00Z",
        "webViewLink": "https://...", "parents": [], "capabilities": {},
    })
    result = await drive_ops.get_file_metadata(svc, "f1")
    assert result["md5_checksum"] == "d41d8cd98f00b204e9800998ecf8427e"


@pytest.mark.asyncio
async def test_get_file_metadata_omits_md5_checksum_for_native_doc():
    svc = _mock_drive_service({
        "id": "f1", "name": "Decision_Log",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-05-19T00:00:00Z",
        "webViewLink": "https://...", "parents": [], "capabilities": {},
    })
    result = await drive_ops.get_file_metadata(svc, "f1")
    assert "md5_checksum" not in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_drive_ops.py -k "size_unavailable or size_accurate or md5_checksum" -v`
Expected: `test_get_file_metadata_native_doc_size_unavailable` FAILs (currently returns `size_bytes: 64707`); the md5 tests FAIL with `KeyError`/missing key.

- [ ] **Step 3: Implement**

Replace `get_file_metadata` in `src/gsuite_mcp/drive_ops.py` (~lines 140-162):

```python
async def get_file_metadata(service, file_id: str) -> dict[str, Any]:
    metadata = await asyncio.to_thread(
        lambda: service.files()
        .get(
            fileId=file_id,
            fields=(
                "id,name,mimeType,size,modifiedTime,webViewLink,parents,"
                "capabilities,trashed,trashedTime,md5Checksum"
            ),
        )
        .execute()
    )
    mime_type = metadata.get("mimeType", "")
    result: dict[str, Any] = {
        "file_id": metadata["id"],
        "name": metadata["name"],
        "mime_type": mime_type,
        "modified_time": metadata.get("modifiedTime", ""),
        "web_view_link": metadata.get("webViewLink", ""),
        "parents": metadata.get("parents", []),
        "capabilities": metadata.get("capabilities", {}),
        "trashed": metadata.get("trashed", False),
    }
    # Drive's `size` field for native Google formats (Docs, Sheets, Slides)
    # reflects internal storage quota usage, not the exported byte count —
    # it can be wildly wrong (observed: reported 64707 vs actual 162908).
    # Returning it as size_bytes would silently poison any byte-comparison
    # gate. Flag it unavailable instead of returning a number that lies.
    if mime_type.startswith("application/vnd.google-apps."):
        result["size_bytes"] = None
        result["size_unavailable"] = True
    else:
        result["size_bytes"] = int(metadata.get("size", 0))
    if metadata.get("md5Checksum"):
        result["md5_checksum"] = metadata["md5Checksum"]
    if metadata.get("trashedTime"):
        result["trashed_time"] = metadata["trashedTime"]
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_drive_ops.py tests/test_metadata.py tests/test_get_files_metadata.py -v`
Expected: all PASS, including the pre-existing `test_get_file_metadata` in `tests/test_metadata.py` (it asserts `size_bytes == 45231` for a `.docx` file, which is not a native Google format, so it's unaffected).

- [ ] **Step 5: Run full suite and commit**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest -q`
Expected: all tests pass.

```bash
cd ~/Desktop/CODING/gsuite-mcp
git add src/gsuite_mcp/drive_ops.py tests/test_drive_ops.py
git commit -m "fix: get_file_metadata returns size_unavailable for native Google formats instead of a wrong size_bytes (D1); expose md5_checksum"
```

---

### Task 3: D2 — revision_id_before/after for append_to_file on native Docs and Sheets

**Files:**
- Modify: `src/gsuite_mcp/server.py` (`append_to_file`, ~line 108)
- Test: `tests/test_append.py` (extend)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `append_to_file`'s response now includes `revision_id_before`/`revision_id_after` when `mode` is `docs_native` or `sheets_native` (absent for `plain_roundtrip`). No other task depends on this.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_append.py`:

```python
@pytest.mark.asyncio
async def test_append_to_google_doc_returns_revision_ids(mock_services):
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().get.return_value.execute.return_value = {
        "name": "Index", "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }
    docs.documents().get.return_value.execute.return_value = {
        "body": {"content": [{"endIndex": 1}, {"endIndex": 42}]}
    }
    docs.documents().batchUpdate.return_value.execute.return_value = {}

    call_count = {"n": 0}
    def revisions_execute():
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"revisions": [{"id": "rev_before"}]}
        return {"revisions": [{"id": "rev_before"}, {"id": "rev_after"}]}
    revisions_mock = MagicMock()
    revisions_mock.execute = revisions_execute
    drive.revisions().list.return_value = revisions_mock

    from gsuite_mcp.server import append_to_file
    result = await append_to_file(file_id="doc123", content="new line", separator="\n")

    assert result["revision_id_before"] == "rev_before"
    assert result["revision_id_after"] == "rev_after"


@pytest.mark.asyncio
async def test_append_to_google_sheet_returns_revision_ids(mock_services):
    drive = mock_services["drive"]
    sheets = mock_services["sheets"]

    drive.files().get.return_value.execute.return_value = {
        "name": "Pipeline", "mimeType": "application/vnd.google-apps.spreadsheet",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }
    sheets.spreadsheets().get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Sheet1"}}],
    }
    sheets.spreadsheets().values().append.return_value.execute.return_value = {
        "updates": {"updatedRange": "Sheet1!A42:C42"}
    }

    call_count = {"n": 0}
    def revisions_execute():
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"revisions": [{"id": "rev1"}]}
        return {"revisions": [{"id": "rev1"}, {"id": "rev2"}]}
    revisions_mock = MagicMock()
    revisions_mock.execute = revisions_execute
    drive.revisions().list.return_value = revisions_mock

    from gsuite_mcp.server import append_to_file
    result = await append_to_file(file_id="sheet123", content="a,b", separator="")

    assert result["revision_id_before"] == "rev1"
    assert result["revision_id_after"] == "rev2"


@pytest.mark.asyncio
async def test_append_to_plain_file_has_no_revision_ids(mock_services):
    drive = mock_services["drive"]

    drive.files().get.return_value.execute.return_value = {
        "name": "notes.md", "mimeType": "text/markdown",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }
    drive.files().get_media.return_value.execute.return_value = b"existing content"
    drive.files().update.return_value.execute.return_value = {
        "id": "plain1", "name": "notes.md", "webViewLink": "https://example.com",
        "version": "2", "modifiedTime": "2026-04-10T12:05:00Z",
    }

    from gsuite_mcp.server import append_to_file
    result = await append_to_file(file_id="plain1", content="new line", separator="\n")

    assert "revision_id_before" not in result
    assert "revision_id_after" not in result
    drive.revisions().list.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_append.py -k revision -v`
Expected: the doc/sheet tests FAIL with `KeyError: 'revision_id_before'`; the plain-file test PASSes already.

- [ ] **Step 3: Implement**

In `src/gsuite_mcp/server.py`, modify `append_to_file` (~lines 108-179). After the trashed check and before the mode dispatch, fetch `revision_id_before` for native formats only; after building the result dict, fetch `revision_id_after` and attach both:

```python
@mcp.tool()
async def append_to_file(
    file_id: str,
    content: str,
    separator: str = "\n",
) -> dict[str, Any]:
    """Append content to a file. Uses native API where possible.

    - Google Docs: Docs API batchUpdate InsertText (preserves formatting)
    - Google Sheets: Sheets API values.append (rows split on newline, cols on comma)
    - Other files: download-concat-upload fallback

    Returns {file_id, file_name, mime_type, bytes_appended, modified_time, mode}.
    For Google Docs/Sheets, also returns revision_id_before/revision_id_after
    (Drive revision IDs) — a monotonic verification handle, unlike
    modified_time, which can lag under Drive API eventual consistency even
    though this tool always re-reads it post-write.
    Refuses trashed files with error: TRASHED_FILE."""
    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(fileId=file_id, fields="name,mimeType,modifiedTime,trashed,trashedTime")
        .execute()
    )
    if meta.get("trashed"):
        return _trashed_error(file_id, meta)
    mime = meta.get("mimeType", "")
    name = meta.get("name", "")

    is_native = mime in (GOOGLE_DOC_MIME, GOOGLE_SHEET_MIME)
    revision_id_before = None
    if is_native:
        rev_resp = await asyncio.to_thread(
            lambda: drive.revisions()
            .list(fileId=file_id, fields="revisions(id)", pageSize=1000)
            .execute()
        )
        revisions = rev_resp.get("revisions", [])
        revision_id_before = revisions[-1]["id"] if revisions else None

    if mime == GOOGLE_DOC_MIME:
        docs = auth.get_docs_service()
        ops_result = await docs_ops.append_text_to_doc(
            docs, file_id, separator + content
        )
        mode = "docs_native"
        # refresh modifiedTime
        meta2 = await asyncio.to_thread(
            lambda: drive.files()
            .get(fileId=file_id, fields="modifiedTime")
            .execute()
        )
        modified_time = meta2.get("modifiedTime", "")
    elif mime == GOOGLE_SHEET_MIME:
        sheets = auth.get_sheets_service()
        ops_result = await sheets_ops.append_rows(sheets, file_id, content)
        mode = "sheets_native"
        meta2 = await asyncio.to_thread(
            lambda: drive.files()
            .get(fileId=file_id, fields="modifiedTime")
            .execute()
        )
        modified_time = meta2.get("modifiedTime", "")
    else:
        # Plain file: download, concat, upload
        current = await drive_ops.download_file_bytes(drive, file_id)
        to_append = (separator + content).encode("utf-8")
        new_bytes = current + to_append
        import base64 as _b64
        upload_result = await drive_ops.upload_file(
            drive,
            content_base64=_b64.b64encode(new_bytes).decode(),
            file_name=name,
            mime_type=mime,
            file_id=file_id,
        )
        mode = "plain_roundtrip"
        modified_time = upload_result.get("modified_time", "")
        ops_result = {"bytes_appended": len(to_append)}

    result = {
        "file_id": file_id,
        "file_name": name,
        "mime_type": mime,
        "bytes_appended": ops_result["bytes_appended"],
        "modified_time": modified_time,
        "mode": mode,
    }
    if is_native:
        rev_resp_after = await asyncio.to_thread(
            lambda: drive.revisions()
            .list(fileId=file_id, fields="revisions(id)", pageSize=1000)
            .execute()
        )
        revisions_after = rev_resp_after.get("revisions", [])
        result["revision_id_before"] = revision_id_before
        result["revision_id_after"] = (
            revisions_after[-1]["id"] if revisions_after else None
        )
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_append.py -v`
Expected: all PASS.

- [ ] **Step 5: Run full suite and commit**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest -q`
Expected: all tests pass.

```bash
cd ~/Desktop/CODING/gsuite-mcp
git add src/gsuite_mcp/server.py tests/test_append.py
git commit -m "feat: append_to_file returns revision_id_before/after for native Docs and Sheets (D2)"
```

---

### Task 4: D9a — drive_ops: shared upload core + upload_file_from_path

**Files:**
- Modify: `src/gsuite_mcp/drive_ops.py` (`upload_file`, ~line 55)
- Test: `tests/test_drive_ops.py` (extend)

**Interfaces:**
- Produces: `drive_ops._upload_media(service, media, file_name, file_id, parent_folder_id, bytes_uploaded) -> dict[str, Any]` and `drive_ops.upload_file_from_path(service, file_path: str, file_name: str, mime_type: str, file_id: Optional[str] = None, parent_folder_id: Optional[str] = None) -> dict[str, Any]`. Task 5 (chunked-upload tools) calls `upload_file_from_path` directly.
- `upload_file`'s existing signature and return shape are unchanged — this is a pure refactor plus one new function.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_drive_ops.py`:

```python
# -------------------------------------------------------------------
# upload_file_from_path
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_file_from_path_creates_new_file(tmp_path):
    svc = MagicMock()
    svc.files().create.return_value.execute.return_value = {
        "id": "new1", "name": "archive.md", "webViewLink": "https://x",
        "version": "1", "modifiedTime": "2026-08-05T00:00:00Z",
    }
    svc.files().get.return_value.execute.return_value = {"size": "11"}

    local_file = tmp_path / "archive.md"
    local_file.write_bytes(b"hello world")

    result = await drive_ops.upload_file_from_path(
        svc, str(local_file), file_name="archive.md", mime_type="text/markdown",
    )
    assert result["file_id"] == "new1"
    assert result["bytes_uploaded"] == 11
    assert result["file_size"] == 11
    svc.files().create.assert_called_once()
    svc.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_upload_file_from_path_updates_existing_file(tmp_path):
    svc = MagicMock()
    svc.files().update.return_value.execute.return_value = {
        "id": "f1", "name": "archive.md", "webViewLink": "https://x",
        "version": "2", "modifiedTime": "2026-08-05T00:00:00Z",
    }
    svc.files().get.return_value.execute.return_value = {"size": "11"}

    local_file = tmp_path / "archive.md"
    local_file.write_bytes(b"hello world")

    result = await drive_ops.upload_file_from_path(
        svc, str(local_file), file_name="archive.md", mime_type="text/markdown",
        file_id="f1",
    )
    assert result["file_id"] == "f1"
    svc.files().update.assert_called_once()
    svc.files().create.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_drive_ops.py -k upload_file_from_path -v`
Expected: FAIL with `AttributeError: module 'gsuite_mcp.drive_ops' has no attribute 'upload_file_from_path'`.

- [ ] **Step 3: Implement the shared core and the new function**

In `src/gsuite_mcp/drive_ops.py`, replace `upload_file` (~lines 55-110) with:

```python
async def _upload_media(
    service,
    media,
    file_name: str,
    file_id: Optional[str],
    parent_folder_id: Optional[str],
    bytes_uploaded: int,
) -> dict[str, Any]:
    if file_id:
        result = await asyncio.to_thread(
            lambda: service.files()
            .update(
                fileId=file_id,
                media_body=media,
                fields="id,name,webViewLink,version,modifiedTime",
            )
            .execute()
        )
    else:
        body: dict[str, Any] = {"name": file_name}
        if parent_folder_id:
            body["parents"] = [parent_folder_id]
        result = await asyncio.to_thread(
            lambda: service.files()
            .create(
                body=body,
                media_body=media,
                fields="id,name,webViewLink,version,modifiedTime",
            )
            .execute()
        )
    # Query actual file size from Drive to let callers detect truncation
    # by comparing bytes_uploaded vs file_size.
    actual_id = result["id"]
    meta = await asyncio.to_thread(
        lambda: service.files()
        .get(fileId=actual_id, fields="size")
        .execute()
    )
    # Native Google formats (Docs, Sheets) don't report size; use
    # bytes_uploaded as the fallback so the comparison is still valid.
    file_size = int(meta["size"]) if "size" in meta else bytes_uploaded
    return {
        "file_id": actual_id,
        "file_name": result["name"],
        "web_view_link": result.get("webViewLink", ""),
        "version": result.get("version", ""),
        "modified_time": result.get("modifiedTime", ""),
        "bytes_uploaded": bytes_uploaded,
        "file_size": file_size,
    }


async def upload_file(
    service,
    content_base64: str,
    file_name: str,
    mime_type: str,
    file_id: Optional[str] = None,
    parent_folder_id: Optional[str] = None,
) -> dict[str, Any]:
    file_bytes = base64.b64decode(content_base64)
    bytes_uploaded = len(file_bytes)
    media = MediaIoBaseUpload(
        io.BytesIO(file_bytes), mimetype=mime_type, resumable=True
    )
    return await _upload_media(
        service, media, file_name, file_id, parent_folder_id, bytes_uploaded
    )


async def upload_file_from_path(
    service,
    file_path: str,
    file_name: str,
    mime_type: str,
    file_id: Optional[str] = None,
    parent_folder_id: Optional[str] = None,
) -> dict[str, Any]:
    """Upload a file already on local disk, streaming it instead of holding
    it as an in-memory base64 payload. Used by the chunked-upload flow
    (upload_file_start/upload_file_chunk/upload_file_finish) once all chunks
    have been assembled into a temp file — see upload_session.py.
    """
    bytes_uploaded = os.path.getsize(file_path)
    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
    return await _upload_media(
        service, media, file_name, file_id, parent_folder_id, bytes_uploaded
    )
```

Update the imports at the top of `src/gsuite_mcp/drive_ops.py`:

```python
"""Google Drive v3 operations — pure async functions that accept a service."""

import asyncio
import base64
import io
import os
from typing import Any, Optional

from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_drive_ops.py -v`
Expected: all PASS, including pre-existing `upload_file`-adjacent tests in `tests/test_trashed_refusal.py`.

- [ ] **Step 5: Run full suite and commit**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest -q`
Expected: all tests pass.

```bash
cd ~/Desktop/CODING/gsuite-mcp
git add src/gsuite_mcp/drive_ops.py tests/test_drive_ops.py
git commit -m "refactor: extract shared upload core in drive_ops, add upload_file_from_path (D9a)"
```

---

### Task 5: D9b — chunked upload: session registry + 3 new MCP tools

**Files:**
- Create: `src/gsuite_mcp/upload_session.py`
- Modify: `src/gsuite_mcp/server.py` (add `upload_file_start`, `upload_file_chunk`, `upload_file_finish`; add `import base64`, `import hashlib`, `from gsuite_mcp import upload_session` to the existing `from gsuite_mcp import ...` line)
- Test: `tests/test_upload_session.py` (create), `tests/test_chunked_upload_tools.py` (create)

**Interfaces:**
- Consumes: `drive_ops.upload_file_from_path` from Task 4.
- Produces: three new MCP tools. `upload_file_start(file_name, mime_type, total_bytes, file_id=None, parent_folder_id=None, expected_sha256=None) -> {upload_id, chunk_size_hint}`; `upload_file_chunk(upload_id, chunk_base64, chunk_index) -> {upload_id, bytes_received, total_bytes}`; `upload_file_finish(upload_id) -> {file_id, file_name, web_view_link, version, modified_time, bytes_uploaded, file_size}` (same shape as `upload_file`'s success response). No other task depends on these.

- [ ] **Step 1: Write the failing tests for the session module**

Create `tests/test_upload_session.py`:

```python
"""Tests for upload_session — in-process chunked-upload state (D9b)."""

import pytest

from gsuite_mcp import upload_session


@pytest.fixture(autouse=True)
def _clear_sessions():
    upload_session._SESSIONS.clear()
    yield
    upload_session._SESSIONS.clear()


def test_start_session_returns_upload_id_and_creates_temp_file():
    result = upload_session.start_session(
        file_name="archive.md", mime_type="text/markdown", total_bytes=11,
    )
    upload_id = result["upload_id"]
    session = upload_session.get_session(upload_id)
    assert session is not None
    assert session["total_bytes"] == 11
    assert session["received_bytes"] == 0


def test_start_session_rejects_non_positive_total_bytes():
    with pytest.raises(ValueError):
        upload_session.start_session(
            file_name="x", mime_type="text/plain", total_bytes=0,
        )


def test_write_chunk_accumulates_bytes_in_order():
    result = upload_session.start_session(
        file_name="archive.md", mime_type="text/markdown", total_bytes=11,
    )
    upload_id = result["upload_id"]
    r1 = upload_session.write_chunk(upload_id, 0, b"hello ")
    assert r1 == {"bytes_received": 6, "total_bytes": 11}
    r2 = upload_session.write_chunk(upload_id, 1, b"world")
    assert r2 == {"bytes_received": 11, "total_bytes": 11}

    session = upload_session.get_session(upload_id)
    with open(session["temp_path"], "rb") as f:
        assert f.read() == b"hello world"


def test_write_chunk_rejects_out_of_order_index():
    result = upload_session.start_session(
        file_name="x", mime_type="text/plain", total_bytes=10,
    )
    upload_id = result["upload_id"]
    with pytest.raises(ValueError):
        upload_session.write_chunk(upload_id, 1, b"skip ahead")


def test_write_chunk_rejects_overflow_past_total_bytes():
    result = upload_session.start_session(
        file_name="x", mime_type="text/plain", total_bytes=5,
    )
    upload_id = result["upload_id"]
    with pytest.raises(ValueError):
        upload_session.write_chunk(upload_id, 0, b"way too long")


def test_write_chunk_unknown_upload_id_raises_keyerror():
    with pytest.raises(KeyError):
        upload_session.write_chunk("nonexistent", 0, b"x")


def test_finish_session_rejects_incomplete_upload():
    result = upload_session.start_session(
        file_name="x", mime_type="text/plain", total_bytes=10,
    )
    upload_id = result["upload_id"]
    upload_session.write_chunk(upload_id, 0, b"short")
    with pytest.raises(ValueError):
        upload_session.finish_session(upload_id)


def test_finish_session_verifies_sha256_when_given():
    import hashlib
    content = b"hello world"
    correct_hash = hashlib.sha256(content).hexdigest()

    result = upload_session.start_session(
        file_name="x", mime_type="text/plain", total_bytes=len(content),
        expected_sha256=correct_hash,
    )
    upload_id = result["upload_id"]
    upload_session.write_chunk(upload_id, 0, content)
    session = upload_session.finish_session(upload_id)
    assert session["received_bytes"] == len(content)


def test_finish_session_rejects_sha256_mismatch():
    result = upload_session.start_session(
        file_name="x", mime_type="text/plain", total_bytes=5,
        expected_sha256="0" * 64,
    )
    upload_id = result["upload_id"]
    upload_session.write_chunk(upload_id, 0, b"hello")
    with pytest.raises(ValueError):
        upload_session.finish_session(upload_id)


def test_finish_session_unknown_upload_id_raises_keyerror():
    with pytest.raises(KeyError):
        upload_session.finish_session("nonexistent")


def test_cleanup_removes_session_and_temp_file():
    result = upload_session.start_session(
        file_name="x", mime_type="text/plain", total_bytes=5,
    )
    upload_id = result["upload_id"]
    session = upload_session.get_session(upload_id)
    temp_path = session["temp_path"]
    import os
    assert os.path.exists(temp_path)

    upload_session.cleanup(upload_id)
    assert upload_session.get_session(upload_id) is None
    assert not os.path.exists(temp_path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_upload_session.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'gsuite_mcp.upload_session'`.

- [ ] **Step 3: Implement `upload_session.py`**

Create `src/gsuite_mcp/upload_session.py`:

```python
"""In-process chunked-upload session tracking (D9b).

The server is otherwise stateless (see CLAUDE.md Key Constraints). This is
a deliberate, scoped exception, added to work around large content_base64
tool-call payloads not surviving model generation intact — a real failure
on 2026-08-05 where two files over 30KB could not be uploaded at all, and
one attempt left a truncated 3KB file in Drive that looked superficially
valid.

Sessions live only in this process's memory plus a per-session temp file on
local disk. They do NOT survive a Cloud Run instance restart or a scale
event that routes a later call to a different instance. That is surfaced
as a loud UPLOAD_NOT_FOUND error in server.py's tool wrappers — never as
silent data loss or a corrupted partial write.
"""

import hashlib
import os
import tempfile
import time
from typing import Any, Optional
from uuid import uuid4

SESSION_TTL_SECONDS = 30 * 60

_SESSIONS: dict[str, dict[str, Any]] = {}


def _purge_expired() -> None:
    now = time.time()
    expired = [
        uid for uid, s in _SESSIONS.items()
        if now - s["created_at"] > SESSION_TTL_SECONDS
    ]
    for uid in expired:
        cleanup(uid)


def start_session(
    file_name: str,
    mime_type: str,
    total_bytes: int,
    file_id: Optional[str] = None,
    parent_folder_id: Optional[str] = None,
    expected_sha256: Optional[str] = None,
) -> dict[str, Any]:
    if total_bytes <= 0:
        raise ValueError("total_bytes must be positive.")
    _purge_expired()
    upload_id = uuid4().hex
    fd, temp_path = tempfile.mkstemp(prefix=f"gsuite_mcp_upload_{upload_id}_")
    os.close(fd)
    _SESSIONS[upload_id] = {
        "temp_path": temp_path,
        "file_name": file_name,
        "mime_type": mime_type,
        "total_bytes": total_bytes,
        "file_id": file_id,
        "parent_folder_id": parent_folder_id,
        "expected_sha256": expected_sha256,
        "received_bytes": 0,
        "next_chunk_index": 0,
        "created_at": time.time(),
    }
    return {"upload_id": upload_id}


def get_session(upload_id: str) -> Optional[dict[str, Any]]:
    return _SESSIONS.get(upload_id)


def write_chunk(upload_id: str, chunk_index: int, chunk_bytes: bytes) -> dict[str, Any]:
    session = _SESSIONS.get(upload_id)
    if session is None:
        raise KeyError(upload_id)
    if chunk_index != session["next_chunk_index"]:
        raise ValueError(
            f"Expected chunk_index {session['next_chunk_index']}, got "
            f"{chunk_index}. Chunks must be sent in order starting at 0."
        )
    new_received = session["received_bytes"] + len(chunk_bytes)
    if new_received > session["total_bytes"]:
        raise ValueError(
            f"Chunk overflows declared total_bytes ({session['total_bytes']}); "
            f"would reach {new_received} bytes."
        )
    with open(session["temp_path"], "ab") as f:
        f.write(chunk_bytes)
    session["received_bytes"] = new_received
    session["next_chunk_index"] += 1
    return {"bytes_received": new_received, "total_bytes": session["total_bytes"]}


def finish_session(upload_id: str) -> dict[str, Any]:
    session = _SESSIONS.get(upload_id)
    if session is None:
        raise KeyError(upload_id)
    if session["received_bytes"] != session["total_bytes"]:
        raise ValueError(
            f"Upload incomplete: received {session['received_bytes']} of "
            f"{session['total_bytes']} declared bytes."
        )
    if session["expected_sha256"] is not None:
        actual = _hash_file(session["temp_path"])
        if actual != session["expected_sha256"]:
            raise ValueError(
                f"sha256 mismatch: expected {session['expected_sha256']}, "
                f"got {actual}."
            )
    return session


def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def cleanup(upload_id: str) -> None:
    session = _SESSIONS.pop(upload_id, None)
    if session is not None:
        try:
            os.unlink(session["temp_path"])
        except OSError:
            pass
```

- [ ] **Step 4: Run session tests to verify they pass**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_upload_session.py -v`
Expected: all PASS.

- [ ] **Step 5: Write the failing tests for the 3 new MCP tools**

Create `tests/test_chunked_upload_tools.py`:

```python
"""Tests for upload_file_start/upload_file_chunk/upload_file_finish (D9b)."""

import base64
import hashlib
from unittest.mock import patch, MagicMock

import pytest

from gsuite_mcp import upload_session


@pytest.fixture(autouse=True)
def _clear_sessions():
    upload_session._SESSIONS.clear()
    yield
    upload_session._SESSIONS.clear()


@pytest.fixture
def mock_drive():
    with patch("gsuite_mcp.auth.get_drive_service") as mock:
        service = MagicMock()
        mock.return_value = service
        yield service


@pytest.mark.asyncio
async def test_upload_file_start_returns_upload_id(mock_drive):
    from gsuite_mcp.server import upload_file_start
    result = await upload_file_start(
        file_name="archive.md", mime_type="text/markdown", total_bytes=11,
    )
    assert "upload_id" in result
    assert "error" not in result


@pytest.mark.asyncio
async def test_upload_file_start_rejects_non_positive_total_bytes(mock_drive):
    from gsuite_mcp.server import upload_file_start
    result = await upload_file_start(
        file_name="x", mime_type="text/plain", total_bytes=0,
    )
    assert result["error"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_upload_file_start_refuses_trashed_file_id(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = {
        "name": "x", "trashed": True, "trashedTime": "2026-08-05T00:00:00Z",
    }
    from gsuite_mcp.server import upload_file_start
    result = await upload_file_start(
        file_name="x", mime_type="text/plain", total_bytes=5, file_id="f1",
    )
    assert result["error"] == "TRASHED_FILE"


@pytest.mark.asyncio
async def test_upload_file_chunk_unknown_id_returns_upload_not_found(mock_drive):
    from gsuite_mcp.server import upload_file_chunk
    result = await upload_file_chunk(
        upload_id="nonexistent", chunk_base64=base64.b64encode(b"x").decode(),
        chunk_index=0,
    )
    assert result["error"] == "UPLOAD_NOT_FOUND"


@pytest.mark.asyncio
async def test_upload_file_finish_end_to_end(mock_drive):
    mock_drive.files().create.return_value.execute.return_value = {
        "id": "new1", "name": "archive.md", "webViewLink": "https://x",
        "version": "1", "modifiedTime": "2026-08-05T00:00:00Z",
    }
    mock_drive.files().get.return_value.execute.return_value = {"size": "11"}

    from gsuite_mcp.server import upload_file_start, upload_file_chunk, upload_file_finish

    start_result = await upload_file_start(
        file_name="archive.md", mime_type="text/markdown", total_bytes=11,
    )
    upload_id = start_result["upload_id"]

    r1 = await upload_file_chunk(
        upload_id=upload_id, chunk_base64=base64.b64encode(b"hello ").decode(),
        chunk_index=0,
    )
    assert r1["bytes_received"] == 6
    r2 = await upload_file_chunk(
        upload_id=upload_id, chunk_base64=base64.b64encode(b"world").decode(),
        chunk_index=1,
    )
    assert r2["bytes_received"] == 11

    finish_result = await upload_file_finish(upload_id=upload_id)
    assert finish_result["file_id"] == "new1"
    assert finish_result["bytes_uploaded"] == 11

    # session is cleaned up after finish
    assert upload_session.get_session(upload_id) is None


@pytest.mark.asyncio
async def test_upload_file_finish_incomplete_upload_errors(mock_drive):
    from gsuite_mcp.server import upload_file_start, upload_file_chunk, upload_file_finish

    start_result = await upload_file_start(
        file_name="x", mime_type="text/plain", total_bytes=11,
    )
    upload_id = start_result["upload_id"]
    await upload_file_chunk(
        upload_id=upload_id, chunk_base64=base64.b64encode(b"short").decode(),
        chunk_index=0,
    )
    result = await upload_file_finish(upload_id=upload_id)
    assert result["error"] == "INCOMPLETE_OR_CORRUPT_UPLOAD"


@pytest.mark.asyncio
async def test_upload_file_finish_unknown_id_returns_upload_not_found(mock_drive):
    from gsuite_mcp.server import upload_file_finish
    result = await upload_file_finish(upload_id="nonexistent")
    assert result["error"] == "UPLOAD_NOT_FOUND"
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_chunked_upload_tools.py -v`
Expected: FAIL with `ImportError` (the three tools don't exist yet).

- [ ] **Step 7: Implement the 3 tools in server.py**

Update the import block at the top of `src/gsuite_mcp/server.py` (~lines 1-16):

```python
"""Google Workspace MCP server — thin wrappers over *_ops modules."""

import asyncio
import base64
import hashlib
import logging
import os
import re
import sys
from typing import Any, Optional

from fastmcp import FastMCP
from googleapiclient.errors import HttpError

from gsuite_mcp import (
    auth,
    docs_ops,
    docx_edits,
    drive_ops,
    gdoc_ops,
    gmail_ops,
    sheets_ops,
    text_ops,
    upload_session,
)
from gsuite_mcp.retry import TRANSIENT_CODES
from gsuite_mcp.api_key_middleware import APIKeyMiddleware
```

Add the three tools directly after `upload_file` (~line 80, before `search_files`):

```python
@mcp.tool()
async def upload_file_start(
    file_name: str,
    mime_type: str,
    total_bytes: int,
    file_id: Optional[str] = None,
    parent_folder_id: Optional[str] = None,
    expected_sha256: Optional[str] = None,
) -> dict[str, Any]:
    """Begin a chunked upload for files too large to send as one upload_file call.

    Large content_base64 payloads generated in a single tool call can arrive
    truncated or corrupted. This spreads the payload across several
    upload_file_chunk calls instead: call upload_file_start once, then
    upload_file_chunk repeatedly with sequential chunk_index starting at 0,
    then upload_file_finish exactly once.

    Sessions expire after 30 minutes of inactivity and do not survive a
    server restart — upload_file_finish (or upload_file_chunk) returns
    UPLOAD_NOT_FOUND if the session is gone; restart from upload_file_start.

    expected_sha256, if given, is checked against the fully-assembled file
    in upload_file_finish before it is written to Drive."""
    if total_bytes <= 0:
        return {
            "error": "INVALID_INPUT",
            "retryable": False,
            "message": "total_bytes must be positive.",
        }
    if file_id:
        drive = auth.get_drive_service()
        meta = await asyncio.to_thread(
            lambda: drive.files()
            .get(fileId=file_id, fields="name,trashed,trashedTime")
            .execute()
        )
        if meta.get("trashed"):
            return _trashed_error(file_id, meta)
    session = upload_session.start_session(
        file_name=file_name,
        mime_type=mime_type,
        total_bytes=total_bytes,
        file_id=file_id,
        parent_folder_id=parent_folder_id,
        expected_sha256=expected_sha256,
    )
    return {"upload_id": session["upload_id"], "chunk_size_hint": 16384}


@mcp.tool()
async def upload_file_chunk(
    upload_id: str,
    chunk_base64: str,
    chunk_index: int,
) -> dict[str, Any]:
    """Send one chunk of a file started with upload_file_start.

    chunk_index must be sequential starting at 0 (0, 1, 2, ...); an
    out-of-order or repeated index is rejected with INVALID_CHUNK."""
    try:
        chunk_bytes = base64.b64decode(chunk_base64, validate=True)
    except Exception:
        return {
            "error": "INVALID_BASE64",
            "retryable": True,
            "message": "chunk_base64 did not decode as valid base64.",
        }
    try:
        result = upload_session.write_chunk(upload_id, chunk_index, chunk_bytes)
    except KeyError:
        return {
            "error": "UPLOAD_NOT_FOUND",
            "retryable": False,
            "message": (
                f"No active upload session {upload_id!r}. It may have "
                f"expired (30 min TTL) or the server restarted. Restart "
                f"with upload_file_start."
            ),
        }
    except ValueError as e:
        return {"error": "INVALID_CHUNK", "retryable": True, "message": str(e)}
    return {"upload_id": upload_id, **result}


@mcp.tool()
async def upload_file_finish(upload_id: str) -> dict[str, Any]:
    """Assemble a chunked upload's bytes and write it to Drive.

    Verifies the assembled byte count matches total_bytes from
    upload_file_start, and the sha256 hash if expected_sha256 was given.
    Returns the same shape as upload_file's success response. The session
    (and its temp file) is deleted whether this succeeds or fails."""
    try:
        session = upload_session.finish_session(upload_id)
    except KeyError:
        return {
            "error": "UPLOAD_NOT_FOUND",
            "retryable": False,
            "message": (
                f"No active upload session {upload_id!r}. It may have "
                f"expired (30 min TTL) or the server restarted. Restart "
                f"with upload_file_start."
            ),
        }
    except ValueError as e:
        return {
            "error": "INCOMPLETE_OR_CORRUPT_UPLOAD",
            "retryable": True,
            "message": str(e),
        }

    try:
        drive = auth.get_drive_service()
        return await drive_ops.upload_file_from_path(
            drive,
            file_path=session["temp_path"],
            file_name=session["file_name"],
            mime_type=session["mime_type"],
            file_id=session["file_id"],
            parent_folder_id=session["parent_folder_id"],
        )
    finally:
        upload_session.cleanup(upload_id)
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_chunked_upload_tools.py -v`
Expected: all PASS.

- [ ] **Step 9: Run full suite and commit**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest -q`
Expected: all tests pass.

```bash
cd ~/Desktop/CODING/gsuite-mcp
git add src/gsuite_mcp/upload_session.py src/gsuite_mcp/server.py tests/test_upload_session.py tests/test_chunked_upload_tools.py
git commit -m "feat: add chunked upload (upload_file_start/chunk/finish) for large payloads (D9)"
```

---

### Task 6: D10 (was D6) — expected_bytes/expected_sha256 guard on upload_file and append_to_file

**Files:**
- Modify: `src/gsuite_mcp/drive_ops.py` (`upload_file`)
- Modify: `src/gsuite_mcp/server.py` (`upload_file` tool wrapper, `append_to_file`)
- Test: `tests/test_drive_ops.py`, `tests/test_append.py` (extend)

**Interfaces:**
- Consumes: `drive_ops.upload_file` and `server.append_to_file` from Tasks 4 and 3 (edits apply on top of those).
- Produces: `upload_file` and `append_to_file` gain optional `expected_bytes: Optional[int]`, `expected_sha256: Optional[str]` params. On mismatch, returns `{"error": "PAYLOAD_SIZE_MISMATCH" | "PAYLOAD_HASH_MISMATCH", ...}` and performs no write / no API calls.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_drive_ops.py`:

```python
# -------------------------------------------------------------------
# upload_file — expected_bytes / expected_sha256 (D10)
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_file_rejects_expected_bytes_mismatch():
    svc = MagicMock()
    content_b64 = base64.b64encode(b"hello world").decode()  # 11 bytes
    result = await drive_ops.upload_file(
        svc, content_b64, "f.txt", "text/plain", expected_bytes=999,
    )
    assert result["error"] == "PAYLOAD_SIZE_MISMATCH"
    assert result["actual_bytes"] == 11
    assert result["expected_bytes"] == 999
    svc.files().create.assert_not_called()
    svc.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_upload_file_rejects_expected_sha256_mismatch():
    svc = MagicMock()
    content_b64 = base64.b64encode(b"hello world").decode()
    result = await drive_ops.upload_file(
        svc, content_b64, "f.txt", "text/plain", expected_sha256="0" * 64,
    )
    assert result["error"] == "PAYLOAD_HASH_MISMATCH"
    svc.files().create.assert_not_called()


@pytest.mark.asyncio
async def test_upload_file_accepts_matching_expected_bytes_and_sha256():
    import hashlib
    content = b"hello world"
    svc = MagicMock()
    svc.files().create.return_value.execute.return_value = {
        "id": "f1", "name": "f.txt", "webViewLink": "https://x",
        "version": "1", "modifiedTime": "2026-08-05T00:00:00Z",
    }
    svc.files().get.return_value.execute.return_value = {"size": "11"}
    result = await drive_ops.upload_file(
        svc, base64.b64encode(content).decode(), "f.txt", "text/plain",
        expected_bytes=11, expected_sha256=hashlib.sha256(content).hexdigest(),
    )
    assert "error" not in result
    assert result["file_id"] == "f1"
```

Add `import base64` at the top of `tests/test_drive_ops.py` if not already present (it isn't currently).

Add to `tests/test_append.py`:

```python
@pytest.mark.asyncio
async def test_append_to_file_rejects_expected_bytes_mismatch(mock_services):
    from gsuite_mcp.server import append_to_file
    result = await append_to_file(
        file_id="doc123", content="new line", expected_bytes=999,
    )
    assert result["error"] == "PAYLOAD_SIZE_MISMATCH"
    mock_services["drive"].files().get.assert_not_called()


@pytest.mark.asyncio
async def test_append_to_file_rejects_expected_sha256_mismatch(mock_services):
    from gsuite_mcp.server import append_to_file
    result = await append_to_file(
        file_id="doc123", content="new line", expected_sha256="0" * 64,
    )
    assert result["error"] == "PAYLOAD_HASH_MISMATCH"
    mock_services["drive"].files().get.assert_not_called()


@pytest.mark.asyncio
async def test_append_to_file_accepts_matching_expected_bytes(mock_services):
    import hashlib
    drive = mock_services["drive"]
    docs = mock_services["docs"]
    drive.files().get.return_value.execute.return_value = {
        "name": "Index", "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }
    docs.documents().get.return_value.execute.return_value = {
        "body": {"content": [{"endIndex": 1}, {"endIndex": 42}]}
    }
    docs.documents().batchUpdate.return_value.execute.return_value = {}
    drive.revisions().list.return_value.execute.return_value = {"revisions": []}

    content = "new line"
    expected_bytes = len(("\n" + content).encode("utf-8"))
    expected_sha256 = hashlib.sha256(("\n" + content).encode("utf-8")).hexdigest()

    from gsuite_mcp.server import append_to_file
    result = await append_to_file(
        file_id="doc123", content=content,
        expected_bytes=expected_bytes, expected_sha256=expected_sha256,
    )
    assert "error" not in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_drive_ops.py tests/test_append.py -k "expected_bytes or expected_sha256" -v`
Expected: all FAIL with `TypeError: ... unexpected keyword argument`.

Note on the `append_to_file` test: `expected_bytes`/`expected_sha256` here validate the exact bytes the server will send to the Docs API — `separator + content` — not just `content`, because that's the payload actually at risk of corruption in transit.

- [ ] **Step 3: Implement**

In `src/gsuite_mcp/drive_ops.py`, modify `upload_file`:

```python
async def upload_file(
    service,
    content_base64: str,
    file_name: str,
    mime_type: str,
    file_id: Optional[str] = None,
    parent_folder_id: Optional[str] = None,
    expected_bytes: Optional[int] = None,
    expected_sha256: Optional[str] = None,
) -> dict[str, Any]:
    file_bytes = base64.b64decode(content_base64)
    bytes_uploaded = len(file_bytes)
    if expected_bytes is not None and bytes_uploaded != expected_bytes:
        return {
            "error": "PAYLOAD_SIZE_MISMATCH",
            "retryable": True,
            "expected_bytes": expected_bytes,
            "actual_bytes": bytes_uploaded,
            "message": (
                f"Received {bytes_uploaded} bytes but expected_bytes was "
                f"{expected_bytes}. The payload may have been truncated in "
                f"transit. Nothing was written."
            ),
        }
    if expected_sha256 is not None:
        actual_hash = hashlib.sha256(file_bytes).hexdigest()
        if actual_hash != expected_sha256:
            return {
                "error": "PAYLOAD_HASH_MISMATCH",
                "retryable": True,
                "expected_sha256": expected_sha256,
                "actual_sha256": actual_hash,
                "message": (
                    "Received payload's sha256 does not match "
                    "expected_sha256. The payload may have been corrupted "
                    "in transit. Nothing was written."
                ),
            }
    media = MediaIoBaseUpload(
        io.BytesIO(file_bytes), mimetype=mime_type, resumable=True
    )
    return await _upload_media(
        service, media, file_name, file_id, parent_folder_id, bytes_uploaded
    )
```

Add `import hashlib` to `src/gsuite_mcp/drive_ops.py`'s imports.

In `src/gsuite_mcp/server.py`, modify the `upload_file` tool wrapper (~line 55) to pass the new params through:

```python
@mcp.tool()
async def upload_file(
    content_base64: str,
    file_name: str,
    mime_type: str,
    file_id: Optional[str] = None,
    parent_folder_id: Optional[str] = None,
    expected_bytes: Optional[int] = None,
    expected_sha256: Optional[str] = None,
) -> dict[str, Any]:
    """Upload a file to Google Drive (create or update).

    expected_bytes/expected_sha256, if given, are checked against the
    decoded content_base64 payload before any Drive API call. On mismatch,
    returns PAYLOAD_SIZE_MISMATCH or PAYLOAD_HASH_MISMATCH and writes
    nothing — this converts silent truncation/corruption of a large
    base64 payload into a loud, retryable failure."""
    if file_id:
        drive = auth.get_drive_service()
        meta = await asyncio.to_thread(
            lambda: drive.files()
            .get(fileId=file_id, fields="name,trashed,trashedTime")
            .execute()
        )
        if meta.get("trashed"):
            return _trashed_error(file_id, meta)
    return await drive_ops.upload_file(
        auth.get_drive_service(),
        content_base64,
        file_name,
        mime_type,
        file_id,
        parent_folder_id,
        expected_bytes=expected_bytes,
        expected_sha256=expected_sha256,
    )
```

In `src/gsuite_mcp/server.py`, modify `append_to_file`'s signature and add the check as the very first step (before the trashed-file lookup, so a bad payload costs zero API calls):

```python
@mcp.tool()
async def append_to_file(
    file_id: str,
    content: str,
    separator: str = "\n",
    expected_bytes: Optional[int] = None,
    expected_sha256: Optional[str] = None,
) -> dict[str, Any]:
    """Append content to a file. Uses native API where possible.

    - Google Docs: Docs API batchUpdate InsertText (preserves formatting)
    - Google Sheets: Sheets API values.append (rows split on newline, cols on comma)
    - Other files: download-concat-upload fallback

    expected_bytes/expected_sha256, if given, are checked against the exact
    bytes about to be sent (separator + content, UTF-8 encoded) before any
    Drive API call. On mismatch, returns PAYLOAD_SIZE_MISMATCH or
    PAYLOAD_HASH_MISMATCH and writes nothing.

    Returns {file_id, file_name, mime_type, bytes_appended, modified_time, mode}.
    For Google Docs/Sheets, also returns revision_id_before/revision_id_after
    (Drive revision IDs) — a monotonic verification handle, unlike
    modified_time, which can lag under Drive API eventual consistency even
    though this tool always re-reads it post-write.
    Refuses trashed files with error: TRASHED_FILE."""
    payload_bytes = (separator + content).encode("utf-8")
    if expected_bytes is not None and len(payload_bytes) != expected_bytes:
        return {
            "error": "PAYLOAD_SIZE_MISMATCH",
            "retryable": True,
            "expected_bytes": expected_bytes,
            "actual_bytes": len(payload_bytes),
            "message": (
                f"Received {len(payload_bytes)} bytes but expected_bytes "
                f"was {expected_bytes}. Nothing was written."
            ),
        }
    if expected_sha256 is not None:
        actual_hash = hashlib.sha256(payload_bytes).hexdigest()
        if actual_hash != expected_sha256:
            return {
                "error": "PAYLOAD_HASH_MISMATCH",
                "retryable": True,
                "expected_sha256": expected_sha256,
                "actual_sha256": actual_hash,
                "message": (
                    "Received content's sha256 does not match "
                    "expected_sha256. Nothing was written."
                ),
            }
    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(fileId=file_id, fields="name,mimeType,modifiedTime,trashed,trashedTime")
        .execute()
    )
    # ... rest of the function body from Task 3 is unchanged from here down ...
```

(`hashlib` is already imported at module level in `server.py` from Task 5, Step 7.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_drive_ops.py tests/test_append.py -v`
Expected: all PASS.

- [ ] **Step 5: Run full suite and commit**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest -q`
Expected: all tests pass.

```bash
cd ~/Desktop/CODING/gsuite-mcp
git add src/gsuite_mcp/drive_ops.py src/gsuite_mcp/server.py tests/test_drive_ops.py tests/test_append.py
git commit -m "feat: expected_bytes/expected_sha256 guard on upload_file and append_to_file (D10)"
```

---

### Task 7: D8 — search_files distinguishes empty results from an unresolved reference

**Files:**
- Modify: `src/gsuite_mcp/drive_ops.py` (`search_files`, ~line 113)
- Test: `tests/test_drive_ops.py` (extend)

**Interfaces:**
- Produces: `search_files` response gains a `status` field: `"results"` (files found), `"empty"` (query resolved, zero matches), or `"unresolved"` (query referenced a `'<id>' in parents` clause whose folder doesn't exist/isn't accessible — carries `unresolved_reference: <id>` and a `message`). Scoped to `search_files` only — other tools in this repo already surface a missing resource as a loud `HttpError`/404 rather than a silently-empty result, so they don't exhibit the ambiguity D8 describes.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_drive_ops.py`:

```python
# -------------------------------------------------------------------
# search_files — empty vs unresolved status (D8)
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_files_returns_results_status_when_files_found():
    svc = MagicMock()
    svc.files().list.return_value.execute.return_value = {
        "files": [{"id": "f1", "name": "a.txt", "mimeType": "text/plain",
                   "modifiedTime": "2026-08-01T00:00:00Z",
                   "webViewLink": "https://x", "parents": []}]
    }
    result = await drive_ops.search_files(svc, "name = 'a.txt'")
    assert result["status"] == "results"


@pytest.mark.asyncio
async def test_search_files_returns_empty_status_for_genuinely_empty_query():
    svc = MagicMock()
    svc.files().list.return_value.execute.return_value = {"files": []}
    result = await drive_ops.search_files(svc, "name = 'nothing_matches_this'")
    assert result["status"] == "empty"
    assert "unresolved_reference" not in result


@pytest.mark.asyncio
async def test_search_files_returns_unresolved_status_for_missing_parent_folder():
    from googleapiclient.errors import HttpError

    svc = MagicMock()
    svc.files().list.return_value.execute.return_value = {"files": []}
    response = MagicMock()
    response.status = 404
    svc.files().get.return_value.execute.side_effect = HttpError(
        response, b"not found"
    )

    result = await drive_ops.search_files(svc, "'bad_folder_id' in parents")
    assert result["status"] == "unresolved"
    assert result["unresolved_reference"] == "bad_folder_id"


@pytest.mark.asyncio
async def test_search_files_returns_empty_status_when_parent_folder_exists_but_empty():
    svc = MagicMock()
    svc.files().list.return_value.execute.return_value = {"files": []}
    svc.files().get.return_value.execute.return_value = {"id": "real_folder"}

    result = await drive_ops.search_files(svc, "'real_folder' in parents")
    assert result["status"] == "empty"
    assert "unresolved_reference" not in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_drive_ops.py -k "status" -v`
Expected: FAIL with `KeyError: 'status'`.

- [ ] **Step 3: Implement**

In `src/gsuite_mcp/drive_ops.py`, add near the top (module level, after imports):

```python
import re

_PARENT_QUERY_RE = re.compile(r"'([^']+)'\s+in\s+parents")
```

Replace `search_files` (~lines 113-137):

```python
async def search_files(service, query: str, max_results: int = 10) -> dict[str, Any]:
    response = await asyncio.to_thread(
        lambda: service.files()
        .list(
            q=query,
            pageSize=max_results,
            fields="files(id,name,mimeType,modifiedTime,webViewLink,parents,trashed,trashedTime)",
        )
        .execute()
    )
    files = []
    for f in response.get("files", []):
        entry = {
            "file_id": f["id"],
            "name": f["name"],
            "mime_type": f.get("mimeType", ""),
            "modified_time": f.get("modifiedTime", ""),
            "web_view_link": f.get("webViewLink", ""),
            "parents": f.get("parents", []),
            "trashed": f.get("trashed", False),
        }
        if f.get("trashedTime"):
            entry["trashed_time"] = f["trashedTime"]
        files.append(entry)

    if files:
        return {"files": files, "status": "results"}

    # Zero results: distinguish a genuinely-empty match from a query that
    # references a parent folder that doesn't exist or isn't accessible —
    # both look identical as a bare empty list otherwise.
    match = _PARENT_QUERY_RE.search(query)
    if match:
        parent_id = match.group(1)
        try:
            await asyncio.to_thread(
                lambda: service.files().get(fileId=parent_id, fields="id").execute()
            )
        except HttpError as exc:
            status = exc.resp.status if exc.resp else 0
            if status == 404:
                return {
                    "files": [],
                    "status": "unresolved",
                    "unresolved_reference": parent_id,
                    "message": (
                        f"Query referenced parent folder {parent_id!r}, "
                        f"which does not exist or is not accessible. Zero "
                        f"results reflects a bad reference, not a "
                        f"genuinely-empty folder."
                    ),
                }
            raise

    return {"files": [], "status": "empty"}
```

Add `from googleapiclient.errors import HttpError` to `src/gsuite_mcp/drive_ops.py`'s imports.

Update the `search_files` tool docstring in `src/gsuite_mcp/server.py` (~line 84):

```python
@mcp.tool()
async def search_files(query: str, max_results: int = 10) -> dict[str, Any]:
    """Search Google Drive for files. Uses Drive API query syntax.

    Zero-result responses carry status: "results" | "empty" | "unresolved".
    "unresolved" applies when the query is a `'<id>' in parents` clause
    whose folder doesn't exist or isn't accessible — distinguishing that
    from a folder that genuinely has no matching files (status: "empty").

    Known Drive API limitation: `name contains 'X'` tokenizes on word
    boundaries and will not match a substring inside a word (e.g. 'eport'
    won't match 'report.docx'). For a substring match, use `name = 'exact
    name'` for an exact match, or list the parent folder
    (`'<folder_id>' in parents`) with max_results above its file count."""
    return await drive_ops.search_files(auth.get_drive_service(), query, max_results)
```

(The docstring's last paragraph also covers D5 — see Task 8, which only needs to verify this text landed; no further code change is needed there.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_drive_ops.py tests/test_search.py -v`
Expected: all PASS. `tests/test_search.py`'s existing `test_search_files` still passes since it only asserts on `result["files"]`, not `status`.

- [ ] **Step 5: Run full suite and commit**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest -q`
Expected: all tests pass.

```bash
cd ~/Desktop/CODING/gsuite-mcp
git add src/gsuite_mcp/drive_ops.py src/gsuite_mcp/server.py tests/test_drive_ops.py
git commit -m "fix: search_files distinguishes empty results from an unresolved parent-folder reference (D8)"
```

---

### Task 8: D5 — document the `name contains` Drive API limitation

**Files:**
- Verify: `src/gsuite_mcp/server.py` (docstring already updated in Task 7, Step 3)
- Test: `tests/test_search.py` (extend)

**Interfaces:**
- Consumes: the `search_files` docstring text written in Task 7. No production code changes in this task — it only adds a regression test locking the documentation in place, since `search_files`'s actual query-matching behavior is a Drive API server-side limitation this codebase cannot fix (it forwards the query string verbatim).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_search.py`:

```python
def test_search_files_docstring_documents_name_contains_limitation():
    from gsuite_mcp.server import search_files
    doc = search_files.__doc__ or ""
    assert "tokenizes" in doc
    assert "name = " in doc
```

- [ ] **Step 2: Run test to verify it fails (or already passes)**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_search.py -k docstring -v`
Expected: PASS already, since Task 7 Step 3 already wrote this docstring text. This step confirms it and locks it in as a regression test — if the docstring is later trimmed, this test catches it.

If it fails (e.g. Task 7 was skipped or reordered), go back to Task 7 Step 3 and apply the `search_files` docstring edit before continuing.

- [ ] **Step 3: Run full suite and commit**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest -q`
Expected: all tests pass.

```bash
cd ~/Desktop/CODING/gsuite-mcp
git add tests/test_search.py
git commit -m "test: lock in search_files' documented name-contains limitation (D5)"
```

---

### Task 9: Update CLAUDE.md and DEPLOYMENT.md, then push and deploy

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/DEPLOYMENT.md` (only if the tool count needs a note — see below)

**Interfaces:**
- Consumes: the final tool count and behavior from Tasks 1-8. No test — this is a documentation task, verified by review rather than pytest.

- [ ] **Step 1: Update the Tools section**

In `CLAUDE.md`, change the intro line:

```diff
-`src/gsuite_mcp/server.py` — FastMCP server exposing 25 tools (refuses to start without `GSUITE_MCP_API_KEY`)
+`src/gsuite_mcp/server.py` — FastMCP server exposing 28 tools (refuses to start without `GSUITE_MCP_API_KEY`)
```

Add a new module bullet for the chunked-upload session registry, next to the `pagination.py` bullet:

```diff
+- `src/gsuite_mcp/upload_session.py` — in-process chunked-upload session state (upload_id -> temp file), a scoped exception to the "no state" constraint; 30-min TTL, never persisted
```

In the numbered `## Tools` list:

```diff
 2. `upload_file` — create or update a file (returns `bytes_uploaded` + `file_size` for truncation detection)
+   Also accepts `expected_bytes`/`expected_sha256` to reject a truncated/corrupted content_base64 payload before writing.
 3. `search_files` — Drive query syntax search
+   Zero-result responses carry `status`: `results` | `empty` | `unresolved` (a `'<id>' in parents` clause referencing a nonexistent folder). Known Drive API limitation: `name contains 'X'` won't match a mid-word substring — documented in the tool description with the `name = 'exact'` workaround.
 4. `get_file_metadata` — single-file metadata
+   For native Google formats (Docs, Sheets, Slides), `size_bytes` is `null` with `size_unavailable: true` instead of Drive's misleading storage-quota number. Exposes `md5_checksum` when Drive provides one.
 6. `append_to_file` — native append for Docs/Sheets; roundtrip fallback for plain files
+   Returns `revision_id_before`/`revision_id_after` for Docs/Sheets (monotonic, unlike `modified_time`, which the tool already re-reads post-write but which can still lag under Drive API eventual consistency). Also accepts `expected_bytes`/`expected_sha256` to reject a corrupted payload before writing.
```

Add three new numbered tool entries after `upload_file` (renumber subsequent entries accordingly, ending at 28):

```
3. `upload_file_start` / `upload_file_chunk` / `upload_file_finish` — chunked upload for files too large to send as one `upload_file` base64 payload. Start declares `total_bytes` (+ optional `expected_sha256`); chunks are sent with sequential `chunk_index` starting at 0; finish assembles, verifies, and writes. Sessions are in-process only, 30-min TTL, `UPLOAD_NOT_FOUND` on loss (never silent).
```

Update the existing `gdoc_batch_replace` and `text_batch_replace` bullets to note key aliasing:

```diff
 18. `gdoc_batch_replace` — batch find/replace in a live Google Doc
+    Each edit accepts either `{find_text, replace_text}` or `{find, replace}` (text_batch_replace's key names) as an alias.
 23. `text_batch_replace` — atomic multi-edit version of `text_replace`
+    Each edit accepts either `{find, replace}` or `{find_text, replace_text}` (gdoc_batch_replace's key names) as an alias.
```

- [ ] **Step 2: Update Key Constraints**

Add to the `## Key Constraints` section:

```diff
+- `upload_file_start`/`upload_file_chunk`/`upload_file_finish` are a deliberate, scoped exception to "no database, no state": chunked-upload sessions live only in process memory plus a per-session temp file, with a 30-minute TTL. They do not survive a Cloud Run instance restart or a scale event that routes a later call to a different instance — that surfaces as a loud `UPLOAD_NOT_FOUND`, never silent data loss. Added because the server has no shared filesystem with any caller (confirmed HTTP-only transport, see `server.py:main()`), so a `source_path`-style parameter cannot work here; large `content_base64` payloads must instead be split across multiple tool calls.
+- `search_threads`-style thread search and Gmail mailbox defaults are **out of scope for this repo** — this codebase has no `search_threads` tool and no Gmail-search-with-mailbox-default tool. Those behaviors belong to the separate built-in Gmail connector, not `gsuite-mcp`.
```

- [ ] **Step 3: Update the Session Tracking line if not already auto-updated by the session hook**

Leave `Total Claude sessions:` / `Last session:` as-is — these are auto-updated by a hook, not hand-maintained.

- [ ] **Step 4: Review the diff**

Run: `cd ~/Desktop/CODING/gsuite-mcp && git diff CLAUDE.md`
Expected: matches the edits above; no unrelated changes.

- [ ] **Step 5: Commit, push, and deploy**

```bash
cd ~/Desktop/CODING/gsuite-mcp
git add CLAUDE.md
git commit -m "docs: document D1/D2/D4/D5/D8/D9/D10 fixes; note D3/D7 out of scope"
git push origin main
./scripts/deploy.sh
```

Confirm the deploy against the live service per `docs/DEPLOYMENT.md`'s smoke-test section before considering this plan complete.

---

## Cover note: what this plan deliberately does NOT do

- **D3** (`search_threads` truncation) and **D7** (single-mailbox default): not actionable in this repo — no such tools exist here. They belong to the built-in Gmail connector.
- **D2**'s literal "stale timestamp" claim: not a bug — `append_to_file` already re-reads `modifiedTime` post-write. Only the `revision_id` addition (Task 3) ships.
- **D9**'s literal "`source_path` on the server": redesigned as chunked upload (Task 5) — the server and every caller are always separated by HTTP, confirmed via `server.py:main()`'s hardcoded `http_app()`/`uvicorn.run()` and this session's own `~/.claude.json` config pointing at the deployed Cloud Run URL. A server-local path parameter would read the *container's* disk, which never has the caller's files, and would add an arbitrary-file-read primitive for no benefit.
