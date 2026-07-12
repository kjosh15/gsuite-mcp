# Server-side text file editing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `text_replace`, `text_batch_replace`, and `text_read_range` tools so plain-text Drive files (`.md`, `.txt`, `.csv`, `.json`, `.yaml`) get the same surgical, guarded, in-place editing that Google Docs already has — without the caller ever transmitting full file contents.

**Architecture:** A new `src/gsuite_mcp/text_ops.py` module holds pure matching/encoding functions plus one shared async orchestration function (`apply_edits_to_file`) implementing the full read→match→guard→write flow. `text_replace` and `text_batch_replace` are both thin `server.py` wrappers over that one function (single-edit vs multi-edit lists). `drive_ops.py` gains one small helper (`download_file_bytes`) that both the new tools and the existing `append_to_file` roundtrip branch share.

**Tech Stack:** Python 3, FastMCP, googleapiclient (Drive v3), pytest + pytest-asyncio (`asyncio_mode = "auto"`).

## Global Constraints

- MIME allowlist: any `text/*` prefix, plus exact `application/json` and `application/x-yaml`. Everything else (including all `application/vnd.google-apps.*` and `application/octet-stream`) is refused with `UNSUPPORTED_MIME`.
- Size ceiling: 5MB (5,242,880 bytes) — `FILE_TOO_LARGE` above that.
- Strict UTF-8 decode, no lossy fallback — `NOT_TEXT_FILE` on `UnicodeDecodeError`.
- Blast-radius guard is active from Phase 1 (reuses `docs_ops.check_blast_radius`, same `BLAST_RADIUS_MIN_DELTA`/`BLAST_RADIUS_MAX_RATIO` env vars as the Google Docs tools).
- Optimistic-concurrency check on every write (compare `modifiedTime`/`md5Checksum` snapshot from read-time vs. immediately before write) — `CONCURRENT_MODIFICATION` on mismatch, write refused.
- No partial writes on any error path — every error dict returns before `drive_ops.upload_file` is called.
- Follow existing conventions exactly: plain dicts for errors (`{"error": CODE, "retryable": bool, "message": str, ...}`), no custom exception classes; `@mcp.tool()` async wrappers in `server.py`; pure functions + orchestration co-located in one `*_ops.py` module (see `docs_ops.py` as precedent).
- TDD: write the failing test before the implementation, for every task.

---

### Task 1: Spike — Drive revisions API reliability for plain-text files

**Files:**
- Create (scratch, not committed): a throwaway script run manually, e.g. `/tmp/revisions_spike.py`

**Interfaces:**
- Consumes: `gsuite_mcp.auth.get_drive_service()` (existing).
- Produces: a documented finding used to annotate the `ALWAYS_BACKUP_ON_WRITE` constant introduced in Task 5. This task does **not** block later tasks — Task 5 ships with `ALWAYS_BACKUP_ON_WRITE = True` (the safe default) regardless of this finding; flipping it to `False` is an optional follow-up once the finding is confirmed.

This is an investigative task, not a TDD code task — there is no failing test to write. It requires live OAuth credentials (`GOOGLE_OAUTH_CLIENT_ID`/`SECRET`/`REFRESH_TOKEN` env vars set), which were not available in the environment this plan was written in. Run it wherever those credentials are available (e.g. locally with `.env` sourced, or against the deployed Cloud Run service's config).

- [ ] **Step 1: Create and run the spike script**

```python
# /tmp/revisions_spike.py
import asyncio
from gsuite_mcp import auth

async def main():
    drive = auth.get_drive_service()

    # Create a small scratch text file.
    created = drive.files().create(
        body={"name": "revisions_spike_scratch.txt"},
        media_body=None,
        fields="id",
    ).execute() if False else None
    # media_body=None isn't valid for create with content; use upload_file's
    # pattern instead via a direct MediaIoBaseUpload.
    import io
    from googleapiclient.http import MediaIoBaseUpload
    media = MediaIoBaseUpload(io.BytesIO(b"v1"), mimetype="text/plain")
    created = drive.files().create(body={"name": "revisions_spike_scratch.txt"}, media_body=media, fields="id").execute()
    file_id = created["id"]
    print("created file_id:", file_id)

    rev1 = drive.files().revisions().list(fileId=file_id, fields="revisions(id)").execute()
    print("revisions after create:", rev1.get("revisions"))

    media2 = MediaIoBaseUpload(io.BytesIO(b"v2 - edited"), mimetype="text/plain")
    drive.files().update(fileId=file_id, media_body=media2).execute()

    rev2 = drive.files().revisions().list(fileId=file_id, fields="revisions(id)").execute()
    print("revisions after update:", rev2.get("revisions"))

    if rev2.get("revisions") and len(rev2["revisions"]) >= 2:
        old_rev_id = rev2["revisions"][0]["id"]
        content = drive.revisions().get(
            fileId=file_id, revisionId=old_rev_id, alt="media"
        ).execute()
        print("old revision content:", content)
        print("FINDING: revisions ARE retained and fetchable for plain text files.")
    else:
        print("FINDING: revisions list did not grow / old content not fetchable — "
              "plain-file revision retention is NOT reliable enough to promise "
              "as a rollback path. ALWAYS_BACKUP_ON_WRITE should stay True.")

    # Cleanup
    drive.files().delete(fileId=file_id).execute()

asyncio.run(main())
```

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run python /tmp/revisions_spike.py`

- [ ] **Step 2: Record the finding**

Add a one-line note to `docs/superpowers/specs/2026-07-11-text-file-editing-design.md` §8 with the actual observed result (revisions reliable / not reliable), and leave `ALWAYS_BACKUP_ON_WRITE = True` in Task 5's code unless the finding clearly supports flipping it — that flip is optional and out of scope for this plan to keep merged.

No commit required for this task (the spike script is scratch; only the one-line doc note, if added, gets committed as part of Task 5's commit).

---

### Task 2: `drive_ops.download_file_bytes` + refactor `append_to_file`

**Files:**
- Modify: `src/gsuite_mcp/drive_ops.py`
- Modify: `src/gsuite_mcp/server.py:155-172` (the `append_to_file` plain-roundtrip branch)
- Test: `tests/test_drive_ops.py`

**Interfaces:**
- Produces: `async def download_file_bytes(service, file_id: str) -> bytes` in `drive_ops.py`. Later tasks (`text_ops.apply_edits_to_file`, `text_ops.read_range`) call this exact signature.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_drive_ops.py`:

```python
@pytest.mark.asyncio
async def test_download_file_bytes_returns_raw_bytes():
    svc = MagicMock()
    svc.files().get_media.return_value.execute.return_value = b"raw file content"
    result = await drive_ops.download_file_bytes(svc, "f1")
    assert result == b"raw file content"
    svc.files().get_media.assert_called_with(fileId="f1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_drive_ops.py::test_download_file_bytes_returns_raw_bytes -v`
Expected: FAIL with `AttributeError: module 'gsuite_mcp.drive_ops' has no attribute 'download_file_bytes'`

- [ ] **Step 3: Implement `download_file_bytes`**

Add to `src/gsuite_mcp/drive_ops.py` (after `download_file`, before `upload_file`):

```python
async def download_file_bytes(service, file_id: str) -> bytes:
    """Download raw file bytes via get_media.

    Thin wrapper for callers that need raw bytes directly rather than
    download_file's base64-wrapped, metadata-carrying result.
    """
    return await asyncio.to_thread(
        lambda: service.files().get_media(fileId=file_id).execute()
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_drive_ops.py::test_download_file_bytes_returns_raw_bytes -v`
Expected: PASS

- [ ] **Step 5: Refactor `append_to_file`'s plain-roundtrip branch to use it**

In `src/gsuite_mcp/server.py`, replace lines 157-159:

```python
        current = await asyncio.to_thread(
            lambda: drive.files().get_media(fileId=file_id).execute()
        )
```

with:

```python
        current = await drive_ops.download_file_bytes(drive, file_id)
```

- [ ] **Step 6: Run the existing append_to_file tests to confirm no behavior change**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_append.py -v`
Expected: PASS (all 3 existing tests, including `test_append_to_plain_file_roundtrips`, unchanged)

- [ ] **Step 7: Commit**

```bash
git add src/gsuite_mcp/drive_ops.py src/gsuite_mcp/server.py tests/test_drive_ops.py docs/superpowers/specs/2026-07-11-text-file-editing-design.md
git commit -m "Add drive_ops.download_file_bytes, refactor append_to_file to use it"
```

---

### Task 3: `text_ops.py` — MIME allowlist, line-ending, decode/encode

**Files:**
- Create: `src/gsuite_mcp/text_ops.py`
- Test: Create `tests/test_text_ops.py`

**Interfaces:**
- Produces:
  - `is_supported_mime(mime_type: str) -> bool`
  - `is_google_apps_mime(mime_type: str) -> bool`
  - `detect_line_ending(text: str) -> str`
  - `decode_text(raw: bytes) -> dict[str, Any]` — returns `{"text": str, "line_ending": str}`; raises `UnicodeDecodeError` (built-in) on invalid UTF-8, not caught here.
  - `encode_text(text: str, line_ending: str) -> bytes`
  - Module constants: `MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024`, `ALLOWED_EXACT_MIME_TYPES`, `GOOGLE_APPS_MIME_PREFIX`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_text_ops.py`:

```python
"""Tests for text_ops pure functions."""

import pytest

from gsuite_mcp import text_ops


class TestIsSupportedMime:
    def test_text_plain_allowed(self):
        assert text_ops.is_supported_mime("text/plain") is True

    def test_text_markdown_allowed(self):
        assert text_ops.is_supported_mime("text/markdown") is True

    def test_application_json_allowed(self):
        assert text_ops.is_supported_mime("application/json") is True

    def test_application_x_yaml_allowed(self):
        assert text_ops.is_supported_mime("application/x-yaml") is True

    def test_google_doc_mime_refused(self):
        assert text_ops.is_supported_mime("application/vnd.google-apps.document") is False

    def test_binary_mime_refused(self):
        assert text_ops.is_supported_mime("application/octet-stream") is False

    def test_pdf_refused(self):
        assert text_ops.is_supported_mime("application/pdf") is False


class TestIsGoogleAppsMime:
    def test_google_doc(self):
        assert text_ops.is_google_apps_mime("application/vnd.google-apps.document") is True

    def test_plain_text(self):
        assert text_ops.is_google_apps_mime("text/plain") is False


class TestDetectLineEnding:
    def test_crlf_detected(self):
        assert text_ops.detect_line_ending("line1\r\nline2\r\n") == "\r\n"

    def test_lf_detected(self):
        assert text_ops.detect_line_ending("line1\nline2\n") == "\n"

    def test_no_newlines_defaults_lf(self):
        assert text_ops.detect_line_ending("no newlines here") == "\n"


class TestDecodeEncodeText:
    def test_decode_valid_utf8(self):
        result = text_ops.decode_text("hello world".encode("utf-8"))
        assert result["text"] == "hello world"
        assert result["line_ending"] == "\n"

    def test_decode_invalid_utf8_raises(self):
        with pytest.raises(UnicodeDecodeError):
            text_ops.decode_text(b"\xff\xfe\x00\x01invalid")

    def test_decode_normalizes_crlf_to_lf(self):
        result = text_ops.decode_text(b"line1\r\nline2\r\n")
        assert result["text"] == "line1\nline2\n"
        assert result["line_ending"] == "\r\n"

    def test_encode_restores_crlf(self):
        raw = text_ops.encode_text("line1\nline2\n", "\r\n")
        assert raw == b"line1\r\nline2\r\n"

    def test_encode_lf_passthrough(self):
        raw = text_ops.encode_text("line1\nline2\n", "\n")
        assert raw == b"line1\nline2\n"

    def test_roundtrip_preserves_crlf(self):
        original = "Status: draft\r\nOwner: josh\r\n"
        decoded = text_ops.decode_text(original.encode("utf-8"))
        re_encoded = text_ops.encode_text(decoded["text"], decoded["line_ending"])
        assert re_encoded.decode("utf-8") == original
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_text_ops.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gsuite_mcp.text_ops'`

- [ ] **Step 3: Create `text_ops.py` with the MIME/encoding functions**

Create `src/gsuite_mcp/text_ops.py`:

```python
"""Plain-text Drive file editing — matching, decoding, and the read-match-write core."""

import asyncio
import base64
import re
from typing import Any

from gsuite_mcp import drive_ops
from gsuite_mcp.docs_ops import check_blast_radius

MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024

ALLOWED_EXACT_MIME_TYPES: set[str] = {"application/json", "application/x-yaml"}
GOOGLE_APPS_MIME_PREFIX = "application/vnd.google-apps."

# Set from the Task 1 revisions-API spike. Drive's revisions().list() may not
# give a reliable rollback point for plain (non-Google-native) files the way
# it does for Google Docs. True is the safe default: every mutating call
# snapshots a backup copy before writing, regardless of blast-radius outcome.
# Flip to False only once the spike confirms revision IDs are a trustworthy
# rollback path for this file type.
ALWAYS_BACKUP_ON_WRITE = True


def is_supported_mime(mime_type: str) -> bool:
    return mime_type.startswith("text/") or mime_type in ALLOWED_EXACT_MIME_TYPES


def is_google_apps_mime(mime_type: str) -> bool:
    return mime_type.startswith(GOOGLE_APPS_MIME_PREFIX)


def detect_line_ending(text: str) -> str:
    """Return '\\r\\n' if any CRLF sequence is present, else '\\n'."""
    return "\r\n" if "\r\n" in text else "\n"


def decode_text(raw: bytes) -> dict[str, Any]:
    """Strictly decode raw bytes as UTF-8. Raises UnicodeDecodeError on failure.

    Internally normalizes CRLF to LF so find/replace patterns don't need to
    account for line-ending style; the original convention is restored by
    encode_text.
    """
    text = raw.decode("utf-8")
    line_ending = detect_line_ending(text)
    normalized = text.replace("\r\n", "\n")
    return {"text": normalized, "line_ending": line_ending}


def encode_text(text: str, line_ending: str) -> bytes:
    out = text.replace("\n", "\r\n") if line_ending == "\r\n" else text
    return out.encode("utf-8")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_text_ops.py -v`
Expected: PASS (all tests in `TestIsSupportedMime`, `TestIsGoogleAppsMime`, `TestDetectLineEnding`, `TestDecodeEncodeText`)

- [ ] **Step 5: Commit**

```bash
git add src/gsuite_mcp/text_ops.py tests/test_text_ops.py
git commit -m "Add text_ops MIME allowlist, line-ending, and UTF-8 encode/decode helpers"
```

---

### Task 4: `text_ops.py` — matching and batch-apply

**Files:**
- Modify: `src/gsuite_mcp/text_ops.py`
- Modify: `tests/test_text_ops.py`

**Interfaces:**
- Consumes: nothing new from prior tasks (pure string functions).
- Produces:
  - `count_matches(content: str, find: str, match_case: bool = True, regex: bool = False) -> int`
  - `apply_replace(content: str, find: str, replace: str, match_case: bool = True, regex: bool = False) -> str`
  - `apply_batch(content: str, edits: list[dict[str, Any]]) -> dict[str, Any]` — returns `{"content": str | None, "per_edit": list[dict], "aborted_at": int | None, "chars_deleted": int, "chars_inserted": int}`. Each edit dict: `{"find": str, "replace": str, "expected_count": int | None, "match_case": bool (default True), "regex": bool (default False)}`. Each `per_edit` entry: `{"index": int, "find_preview": str, "matches_found": int, "applied": bool}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_text_ops.py`:

```python
class TestCountAndApplyReplace:
    def test_count_matches_literal(self):
        assert text_ops.count_matches("abc abc abc", "abc") == 3

    def test_count_matches_case_insensitive(self):
        assert text_ops.count_matches("ABC abc AbC", "abc", match_case=False) == 3

    def test_count_matches_regex(self):
        assert text_ops.count_matches("v1.0 v2.0 v3.0", r"v\d\.0", regex=True) == 3

    def test_apply_replace_literal(self):
        assert text_ops.apply_replace("hello world", "world", "there") == "hello there"

    def test_apply_replace_case_insensitive(self):
        result = text_ops.apply_replace("Status: OLD", "old", "NEW", match_case=False)
        assert result == "Status: NEW"

    def test_apply_replace_regex(self):
        result = text_ops.apply_replace("v1.0", r"v(\d)\.0", r"v\1.1", regex=True)
        assert result == "v1.1"

    def test_apply_replace_multiline_pattern(self):
        content = "## Status\nold line\n## Next"
        result = text_ops.apply_replace(content, "## Status\nold line", "## Status\nnew line")
        assert result == "## Status\nnew line\n## Next"

    def test_apply_replace_no_match_returns_unchanged(self):
        assert text_ops.apply_replace("hello", "xyz", "abc") == "hello"


class TestApplyBatch:
    def test_single_edit_applies(self):
        result = text_ops.apply_batch("hello world", [
            {"find": "world", "replace": "there", "expected_count": 1},
        ])
        assert result["content"] == "hello there"
        assert result["aborted_at"] is None
        assert result["per_edit"] == [
            {"index": 0, "find_preview": "world", "matches_found": 1, "applied": True},
        ]
        assert result["chars_deleted"] == 5
        assert result["chars_inserted"] == 5

    def test_sequential_edits_see_prior_result(self):
        """Edit 2 must operate on the output of edit 1, not the original."""
        result = text_ops.apply_batch("A B", [
            {"find": "A", "replace": "A-X", "expected_count": 1},
            {"find": "A-X", "replace": "A-X-Y", "expected_count": 1},
        ])
        assert result["content"] == "A-X-Y B"
        assert result["aborted_at"] is None

    def test_count_mismatch_aborts_before_any_write(self):
        result = text_ops.apply_batch("foo foo", [
            {"find": "foo", "replace": "bar", "expected_count": 1},
        ])
        assert result["content"] is None
        assert result["aborted_at"] == 0
        assert result["per_edit"][0]["matches_found"] == 2
        assert result["per_edit"][0]["applied"] is False

    def test_batch_aborts_at_failing_edit_leaves_content_none(self):
        """7 edits, edit index 3 fails — no edit (including 0-2) is reflected in content."""
        edits = [{"find": f"marker{i}", "replace": f"replaced{i}", "expected_count": 1} for i in range(7)]
        content = "marker0 marker1 marker2 marker3 marker3 marker4 marker5 marker6"
        result = text_ops.apply_batch(content, edits)
        assert result["content"] is None
        assert result["aborted_at"] == 3
        assert result["per_edit"][3]["matches_found"] == 2
        assert len(result["per_edit"]) == 4  # edits 0,1,2 succeeded, 3 failed, 4-6 never attempted

    def test_no_expected_count_never_aborts(self):
        result = text_ops.apply_batch("foo foo foo", [
            {"find": "foo", "replace": "bar"},
        ])
        assert result["content"] == "bar bar bar"
        assert result["aborted_at"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_text_ops.py -k "CountAndApplyReplace or ApplyBatch" -v`
Expected: FAIL — `AttributeError: module 'gsuite_mcp.text_ops' has no attribute 'count_matches'`

- [ ] **Step 3: Implement matching + apply_batch**

Append to `src/gsuite_mcp/text_ops.py`:

```python
def _find_spans(content: str, find: str, match_case: bool, regex: bool) -> list[tuple[int, int]]:
    if regex:
        flags = 0 if match_case else re.IGNORECASE
        return [(m.start(), m.end()) for m in re.finditer(find, content, flags)]
    haystack = content if match_case else content.casefold()
    needle = find if match_case else find.casefold()
    spans: list[tuple[int, int]] = []
    idx = 0
    while True:
        idx = haystack.find(needle, idx)
        if idx == -1:
            break
        spans.append((idx, idx + len(find)))
        idx += 1
    return spans


def count_matches(content: str, find: str, match_case: bool = True, regex: bool = False) -> int:
    return len(_find_spans(content, find, match_case, regex))


def apply_replace(content: str, find: str, replace: str, match_case: bool = True, regex: bool = False) -> str:
    if regex:
        flags = 0 if match_case else re.IGNORECASE
        return re.sub(find, replace, content, flags=flags)
    spans = _find_spans(content, find, match_case, regex=False)
    if not spans:
        return content
    pieces: list[str] = []
    last = 0
    for start, end in spans:
        pieces.append(content[last:start])
        pieces.append(replace)
        last = end
    pieces.append(content[last:])
    return "".join(pieces)


def apply_batch(content: str, edits: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply edits sequentially; abort all-or-nothing on the first expected_count mismatch.

    Edit N sees the result of edits 1..N-1, matching gdoc_batch_replace's
    documented contract. content is None when aborted — caller must not write.
    """
    current = content
    per_edit: list[dict[str, Any]] = []
    total_deleted = 0
    total_inserted = 0
    for i, edit in enumerate(edits):
        find = edit["find"]
        replace = edit["replace"]
        match_case = edit.get("match_case", True)
        regex = edit.get("regex", False)
        expected_count = edit.get("expected_count")
        actual = count_matches(current, find, match_case=match_case, regex=regex)

        if expected_count is not None and actual != expected_count:
            per_edit.append({
                "index": i,
                "find_preview": find[:80],
                "matches_found": actual,
                "applied": False,
            })
            return {
                "content": None,
                "per_edit": per_edit,
                "aborted_at": i,
                "chars_deleted": 0,
                "chars_inserted": 0,
            }

        new_content = apply_replace(current, find, replace, match_case=match_case, regex=regex)
        if not regex:
            total_deleted += len(find) * actual
            total_inserted += len(replace) * actual
        else:
            # Regex replacement length varies with backreferences; measure
            # the whole-buffer length delta instead of per-match precision.
            delta = len(new_content) - len(current)
            total_inserted += max(delta, 0)
            total_deleted += max(-delta, 0)
        current = new_content
        per_edit.append({
            "index": i,
            "find_preview": find[:80],
            "matches_found": actual,
            "applied": True,
        })

    return {
        "content": current,
        "per_edit": per_edit,
        "aborted_at": None,
        "chars_deleted": total_deleted,
        "chars_inserted": total_inserted,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_text_ops.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add src/gsuite_mcp/text_ops.py tests/test_text_ops.py
git commit -m "Add text_ops matching and sequential all-or-nothing batch-apply"
```

---

### Task 5: `text_ops.apply_edits_to_file` — the shared read-match-guard-write core

This is the safety-critical core both `text_replace` and `text_batch_replace` call. Test it directly against a mocked `drive_service`, independent of the `server.py` wrappers built in Tasks 6-7.

**Files:**
- Modify: `src/gsuite_mcp/text_ops.py`
- Create: `tests/test_apply_edits_to_file.py`

**Interfaces:**
- Consumes: `drive_ops.download_file_bytes` (Task 2), `drive_ops.upload_file` (existing), `drive_ops.create_backup_copy` (existing), `docs_ops.check_blast_radius` (existing), `apply_batch`/`decode_text`/`encode_text`/`is_supported_mime`/`is_google_apps_mime`/`MAX_FILE_SIZE_BYTES`/`ALWAYS_BACKUP_ON_WRITE` (Tasks 3-4).
- Produces:

```python
async def apply_edits_to_file(
    drive_service,
    file_id: str,
    meta: dict[str, Any],
    edits: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    confirm_delete_chars: int | None = None,
    blast_min_delta: int = 200,
    blast_max_ratio: float = 2.0,
    backup_folder_id: str | None = None,
) -> dict[str, Any]
```

`meta` must already carry `mimeType`, `size`, `modifiedTime`, `md5Checksum`, `name` from a fresh `files().get()` call by the caller (the caller also does the `TRASHED_FILE` check before calling this — trashed files never reach this function). Later tasks (`text_replace`, `text_batch_replace` tool wrappers) call this exact signature and merge `file_id`/`file_name`/`mime_type` into the result themselves.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_apply_edits_to_file.py`:

```python
"""Tests for text_ops.apply_edits_to_file — the core read-match-guard-write flow."""

import base64
from unittest.mock import MagicMock

import pytest

from gsuite_mcp import text_ops


def _meta(**overrides):
    base = {
        "name": "notes.md",
        "mimeType": "text/markdown",
        "size": "20",
        "modifiedTime": "2026-07-10T00:00:00Z",
        "md5Checksum": "abc123",
    }
    base.update(overrides)
    return base


def _mock_drive(initial_content: bytes, *, concurrency_meta: dict | None = None):
    """A MagicMock drive service wired for the happy path.

    files().get_media() -> initial_content
    files().get()       -> a superset dict serving both the backup helper's
                            name/parents lookup and the concurrency recheck's
                            modifiedTime/md5Checksum lookup
    files().update()    -> upload_file's write path
    files().copy()      -> backup helper
    revisions().list()  -> called twice (before/after); returns growing ids
    """
    svc = MagicMock()
    svc.files().get_media.return_value.execute.return_value = initial_content

    get_return = {
        "name": "notes.md",
        "parents": ["folder1"],
        "modifiedTime": "2026-07-10T00:00:00Z",
        "md5Checksum": "abc123",
        "size": "999",
    }
    if concurrency_meta is not None:
        get_return.update(concurrency_meta)
    svc.files().get.return_value.execute.return_value = get_return

    svc.files().update.return_value.execute.return_value = {
        "id": "f1", "name": "notes.md", "webViewLink": "https://x",
        "version": "2", "modifiedTime": "2026-07-10T01:00:00Z",
    }
    svc.files().copy.return_value.execute.return_value = {
        "id": "backup1", "name": "notes.md__autobackup_2026-07-10T00:00:00Z",
    }

    call_count = {"n": 0}
    def revisions_execute():
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"revisions": [{"id": "rev_before"}]}
        return {"revisions": [{"id": "rev_before"}, {"id": "rev_after"}]}
    revisions_mock = MagicMock()
    revisions_mock.execute = revisions_execute
    svc.revisions().list.return_value = revisions_mock

    return svc


@pytest.mark.asyncio
async def test_unsupported_mime_refused_before_download():
    svc = _mock_drive(b"content")
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(mimeType="application/vnd.google-apps.document"),
        edits=[{"find": "x", "replace": "y"}],
    )
    assert result["error"] == "UNSUPPORTED_MIME"
    assert "replace_text" in result["message"]
    svc.files().get_media.assert_not_called()


@pytest.mark.asyncio
async def test_file_too_large_refused():
    svc = _mock_drive(b"content")
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(size=str(text_ops.MAX_FILE_SIZE_BYTES + 1)),
        edits=[{"find": "x", "replace": "y"}],
    )
    assert result["error"] == "FILE_TOO_LARGE"
    svc.files().get_media.assert_not_called()


@pytest.mark.asyncio
async def test_not_text_file_refused():
    svc = _mock_drive(b"\xff\xfe\x00\x01invalid")
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(), edits=[{"find": "x", "replace": "y"}],
    )
    assert result["error"] == "NOT_TEXT_FILE"
    svc.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_count_mismatch_no_write():
    svc = _mock_drive(b"foo foo")
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(),
        edits=[{"find": "foo", "replace": "bar", "expected_count": 1}],
    )
    assert result["error"] == "COUNT_MISMATCH"
    assert result["matches_found"] == 2
    svc.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_no_match_error_code():
    svc = _mock_drive(b"hello world")
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(),
        edits=[{"find": "xyz", "replace": "abc", "expected_count": 1}],
    )
    assert result["error"] == "NO_MATCH"


@pytest.mark.asyncio
async def test_batch_abort_names_failing_edit_no_write():
    svc = _mock_drive(b"marker0 marker1 marker1")
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(),
        edits=[
            {"find": "marker0", "replace": "X", "expected_count": 1},
            {"find": "marker1", "replace": "Y", "expected_count": 1},
        ],
    )
    assert result["error"] == "BATCH_ABORTED"
    assert result["failed_edit_index"] == 1
    svc.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_writes_nothing():
    svc = _mock_drive(b"hello world")
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(),
        edits=[{"find": "world", "replace": "there", "expected_count": 1}],
        dry_run=True,
    )
    assert result["dry_run"] is True
    assert result["matches_found"] == 1
    svc.files().update.assert_not_called()
    svc.files().copy.assert_not_called()


@pytest.mark.asyncio
async def test_blast_radius_trips_without_confirm():
    big_find = "x" * 500
    content = big_find + " tail"
    svc = _mock_drive(content.encode())
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(),
        edits=[{"find": big_find, "replace": "", "expected_count": 1}],
    )
    assert result["error"] == "BLAST_RADIUS_EXCEEDED"
    svc.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_confirmed_blast_radius_creates_backup_and_writes():
    big_find = "x" * 500
    content = big_find + " tail"
    svc = _mock_drive(content.encode())
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(),
        edits=[{"find": big_find, "replace": "", "expected_count": 1}],
        confirm_delete_chars=500,
    )
    assert "error" not in result
    assert result["backup_file_id"] == "backup1"
    svc.files().update.assert_called_once()


@pytest.mark.asyncio
async def test_concurrent_modification_detected_no_write():
    svc = _mock_drive(
        b"hello world",
        concurrency_meta={"modifiedTime": "2026-07-10T02:00:00Z", "md5Checksum": "changed"},
    )
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(),
        edits=[{"find": "world", "replace": "there", "expected_count": 1}],
    )
    assert result["error"] == "CONCURRENT_MODIFICATION"
    svc.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_successful_edit_returns_revision_ids_and_diff_counts():
    content = b"Status: old\r\nOwner: josh\r\n"
    svc = _mock_drive(content)
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(),
        edits=[{"find": "Status: old", "replace": "Status: new", "expected_count": 1}],
    )
    assert "error" not in result
    assert result["matches_found"] == 1
    assert result["revision_id_before"] == "rev_before"
    assert result["revision_id_after"] == "rev_after"
    assert result["chars_deleted"] == len("Status: old")
    assert result["chars_inserted"] == len("Status: new")
    svc.files().update.assert_called_once()


@pytest.mark.asyncio
async def test_successful_edit_preserves_crlf(monkeypatch):
    content = b"Status: old\r\nOwner: josh\r\n"
    svc = _mock_drive(content)

    captured = {}
    async def fake_upload_file(service, content_base64, file_name, mime_type, file_id=None, parent_folder_id=None):
        captured["bytes"] = base64.b64decode(content_base64)
        return {"file_id": file_id, "file_name": file_name, "modified_time": "2026-07-10T01:00:00Z",
                "bytes_uploaded": len(captured["bytes"]), "file_size": len(captured["bytes"])}

    monkeypatch.setattr(text_ops.drive_ops, "upload_file", fake_upload_file)

    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(),
        edits=[{"find": "Status: old", "replace": "Status: new", "expected_count": 1}],
    )
    assert "error" not in result
    assert captured["bytes"] == b"Status: new\r\nOwner: josh\r\n"


@pytest.mark.asyncio
async def test_successful_batch_edit_applies_all_pairs():
    svc = _mock_drive(b"one two three")
    result = await text_ops.apply_edits_to_file(
        svc, "f1", _meta(),
        edits=[
            {"find": "one", "replace": "1", "expected_count": 1},
            {"find": "two", "replace": "2", "expected_count": 1},
            {"find": "three", "replace": "3", "expected_count": 1},
        ],
    )
    assert "error" not in result
    assert len(result["per_edit"]) == 3
    assert all(e["applied"] for e in result["per_edit"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_apply_edits_to_file.py -v`
Expected: FAIL — `AttributeError: module 'gsuite_mcp.text_ops' has no attribute 'apply_edits_to_file'`

- [ ] **Step 3: Implement `apply_edits_to_file`**

Append to `src/gsuite_mcp/text_ops.py`:

```python
async def apply_edits_to_file(
    drive_service,
    file_id: str,
    meta: dict[str, Any],
    edits: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    confirm_delete_chars: int | None = None,
    blast_min_delta: int = 200,
    blast_max_ratio: float = 2.0,
    backup_folder_id: str | None = None,
) -> dict[str, Any]:
    """Shared read-match-guard-write core for text_replace/text_batch_replace.

    meta must already carry mimeType, size, modifiedTime, md5Checksum, name
    from a fresh files().get() call by the caller; the caller also performs
    the TRASHED_FILE check before calling this. Every error path below
    returns before any write occurs.
    """
    mime_type = meta.get("mimeType", "")
    if not is_supported_mime(mime_type):
        message = (
            "text_replace/text_batch_replace only work on plain-text files "
            "(text/*, application/json, application/x-yaml). This file is "
            f"{mime_type}."
        )
        if is_google_apps_mime(mime_type):
            message += " For Google Docs, use replace_text or gdoc_batch_replace."
        return {"error": "UNSUPPORTED_MIME", "retryable": False, "message": message}

    size = int(meta.get("size") or 0)
    if size > MAX_FILE_SIZE_BYTES:
        return {
            "error": "FILE_TOO_LARGE",
            "retryable": False,
            "size_bytes": size,
            "message": (
                f"File is {size} bytes; text_replace/text_batch_replace refuse "
                f"files over {MAX_FILE_SIZE_BYTES} bytes."
            ),
        }

    raw = await drive_ops.download_file_bytes(drive_service, file_id)
    try:
        decoded = decode_text(raw)
    except UnicodeDecodeError:
        return {
            "error": "NOT_TEXT_FILE",
            "retryable": False,
            "message": "File content is not valid UTF-8 text.",
        }

    batch_result = apply_batch(decoded["text"], edits)
    per_edit = batch_result["per_edit"]

    if batch_result["aborted_at"] is not None:
        failed = per_edit[-1]
        if len(edits) > 1:
            error_code = "BATCH_ABORTED"
        elif failed["matches_found"] == 0:
            error_code = "NO_MATCH"
        else:
            error_code = "COUNT_MISMATCH"
        return {
            "error": error_code,
            "retryable": True,
            "failed_edit_index": failed["index"],
            "matches_found": failed["matches_found"],
            "per_edit": per_edit,
            "message": (
                f"Edit {failed['index']} ('{failed['find_preview']}') expected "
                f"{edits[failed['index']].get('expected_count')} match(es) but "
                f"found {failed['matches_found']}. No changes made."
            ),
        }

    new_text = batch_result["content"]
    chars_deleted = batch_result["chars_deleted"]
    chars_inserted = batch_result["chars_inserted"]

    if dry_run:
        return {
            "dry_run": True,
            "matches_found": per_edit[0]["matches_found"] if len(edits) == 1 else None,
            "per_edit": per_edit,
            "bytes_before": len(raw),
            "chars_deleted": chars_deleted,
            "chars_inserted": chars_inserted,
            "net_change": chars_inserted - chars_deleted,
        }

    blast = check_blast_radius(
        chars_deleted=chars_deleted,
        chars_inserted=chars_inserted,
        confirm_delete_chars=confirm_delete_chars,
        min_delta=blast_min_delta,
        max_ratio=blast_max_ratio,
    )
    if blast is not None:
        blast["per_edit"] = per_edit
        return blast

    backup_file_id = None
    backup_file_name = None
    if confirm_delete_chars is not None or ALWAYS_BACKUP_ON_WRITE:
        backup = await drive_ops.create_backup_copy(
            drive_service, file_id, backup_folder_id=backup_folder_id,
        )
        backup_file_id = backup["backup_file_id"]
        backup_file_name = backup["backup_file_name"]

    # Optimistic-concurrency check: re-fetch immediately before writing.
    current = await asyncio.to_thread(
        lambda: drive_service.files()
        .get(fileId=file_id, fields="modifiedTime,md5Checksum")
        .execute()
    )
    if (
        current.get("modifiedTime") != meta.get("modifiedTime")
        or current.get("md5Checksum") != meta.get("md5Checksum")
    ):
        return {
            "error": "CONCURRENT_MODIFICATION",
            "retryable": True,
            "message": (
                "File changed since it was read. Re-read the file to get "
                "current content before retrying this edit."
            ),
        }

    new_bytes = encode_text(new_text, decoded["line_ending"])

    # Best-effort revision anchor; not all Drive files retain content
    # revisions the way Google Docs do (see ALWAYS_BACKUP_ON_WRITE above).
    rev_resp = await asyncio.to_thread(
        lambda: drive_service.revisions()
        .list(fileId=file_id, fields="revisions(id)", pageSize=1000)
        .execute()
    )
    revisions = rev_resp.get("revisions", [])
    revision_id_before = revisions[-1]["id"] if revisions else None

    upload_result = await drive_ops.upload_file(
        drive_service,
        content_base64=base64.b64encode(new_bytes).decode(),
        file_name=meta.get("name", ""),
        mime_type=mime_type,
        file_id=file_id,
    )

    rev_resp_after = await asyncio.to_thread(
        lambda: drive_service.revisions()
        .list(fileId=file_id, fields="revisions(id)", pageSize=1000)
        .execute()
    )
    revisions_after = rev_resp_after.get("revisions", [])
    revision_id_after = revisions_after[-1]["id"] if revisions_after else None

    result: dict[str, Any] = {
        "matches_found": per_edit[0]["matches_found"] if len(edits) == 1 else None,
        "per_edit": per_edit,
        "bytes_before": len(raw),
        "bytes_after": len(new_bytes),
        "chars_deleted": chars_deleted,
        "chars_inserted": chars_inserted,
        "net_change": chars_inserted - chars_deleted,
        "revision_id_before": revision_id_before,
        "revision_id_after": revision_id_after,
        "modified_time": upload_result.get("modified_time", ""),
    }
    if backup_file_id:
        result["backup_file_id"] = backup_file_id
        result["backup_file_name"] = backup_file_name
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_apply_edits_to_file.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest -q`
Expected: PASS, all prior tests still green

- [ ] **Step 6: Commit**

```bash
git add src/gsuite_mcp/text_ops.py tests/test_apply_edits_to_file.py
git commit -m "Add text_ops.apply_edits_to_file: guarded read-match-write core"
```

---

### Task 6: `server.py` — `text_replace` tool

**Files:**
- Modify: `src/gsuite_mcp/server.py`
- Create: `tests/test_text_replace.py`

**Interfaces:**
- Consumes: `text_ops.apply_edits_to_file` (Task 5), `_trashed_error` (existing, `server.py:26`).
- Produces: `@mcp.tool() async def text_replace(file_id, find, replace, expected_count=None, match_case=True, regex=False, dry_run=False, confirm_delete_chars=None) -> dict`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_text_replace.py`:

```python
"""Tests for the text_replace tool wrapper in server.py."""

from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def mock_drive():
    with patch("gsuite_mcp.auth.get_drive_service") as mock:
        drive = MagicMock()
        mock.return_value = drive
        yield drive


def _live_meta(**overrides):
    base = {
        "name": "notes.md",
        "mimeType": "text/markdown",
        "size": "11",
        "modifiedTime": "2026-07-10T00:00:00Z",
        "md5Checksum": "abc123",
        "trashed": False,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_text_replace_refuses_trashed(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = _live_meta(
        trashed=True, trashedTime="2026-07-01T00:00:00Z",
    )
    from gsuite_mcp.server import text_replace
    result = await text_replace("f1", "old", "new")
    assert result["error"] == "TRASHED_FILE"


@pytest.mark.asyncio
async def test_text_replace_refuses_google_doc(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = _live_meta(
        mimeType="application/vnd.google-apps.document",
    )
    from gsuite_mcp.server import text_replace
    result = await text_replace("f1", "old", "new")
    assert result["error"] == "UNSUPPORTED_MIME"
    assert "replace_text" in result["message"]


@pytest.mark.asyncio
async def test_text_replace_success(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = _live_meta()
    mock_drive.files().get_media.return_value.execute.return_value = b"hello world"
    mock_drive.files().update.return_value.execute.return_value = {
        "id": "f1", "name": "notes.md", "webViewLink": "https://x",
        "version": "2", "modifiedTime": "2026-07-10T01:00:00Z",
    }
    call_count = {"n": 0}
    def revisions_execute():
        call_count["n"] += 1
        return {"revisions": [{"id": f"rev{call_count['n']}"}]}
    revisions_mock = MagicMock()
    revisions_mock.execute = revisions_execute
    mock_drive.revisions().list.return_value = revisions_mock

    from gsuite_mcp.server import text_replace
    result = await text_replace(
        "f1", "world", "there", expected_count=1,
    )
    assert "error" not in result
    assert result["file_id"] == "f1"
    assert result["file_name"] == "notes.md"
    assert result["mime_type"] == "text/markdown"
    assert result["matches_found"] == 1
    mock_drive.files().update.assert_called_once()


@pytest.mark.asyncio
async def test_text_replace_count_mismatch_no_write(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = _live_meta(size="7")
    mock_drive.files().get_media.return_value.execute.return_value = b"foo foo"

    from gsuite_mcp.server import text_replace
    result = await text_replace("f1", "foo", "bar", expected_count=1)
    assert result["error"] == "COUNT_MISMATCH"
    mock_drive.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_text_replace_dry_run(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = _live_meta()
    mock_drive.files().get_media.return_value.execute.return_value = b"hello world"

    from gsuite_mcp.server import text_replace
    result = await text_replace("f1", "world", "there", dry_run=True)
    assert result["dry_run"] is True
    assert result["matches_found"] == 1
    mock_drive.files().update.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_text_replace.py -v`
Expected: FAIL — `ImportError: cannot import name 'text_replace' from 'gsuite_mcp.server'`

- [ ] **Step 3: Add the `text_ops` import and `text_replace` tool**

In `src/gsuite_mcp/server.py`, update the import line (line 13):

```python
from gsuite_mcp import auth, docs_ops, docx_edits, drive_ops, gdoc_ops, gmail_ops, sheets_ops, text_ops
```

Add the tool after `gdoc_batch_replace` (after line 862, before `trash_file`):

```python
@mcp.tool()
async def text_replace(
    file_id: str,
    find: str,
    replace: str,
    expected_count: Optional[int] = None,
    match_case: bool = True,
    regex: bool = False,
    dry_run: bool = False,
    confirm_delete_chars: Optional[int] = None,
) -> dict[str, Any]:
    """Surgical find/replace in a plain-text Drive file (.md, .txt, .csv, .json, .yaml).

    Downloads, edits, and re-uploads server-side — the caller never needs to
    transmit the file's full contents. Matching operates on the raw text
    stream: newlines are ordinary characters, so multi-line find patterns
    work naturally (unlike replace_text on Google Docs, which is
    paragraph-bound).

    expected_count is checked before any write: on mismatch, returns
    COUNT_MISMATCH (or NO_MATCH if zero matches) with the actual count, and
    writes nothing. dry_run=True returns matches_found without writing.

    Large deletions trip a blast-radius guard (BLAST_RADIUS_EXCEEDED); pass
    confirm_delete_chars=<chars_deleted> from the error to proceed. A
    confirmed edit auto-snapshots a backup copy before writing.

    Detects concurrent edits: if the file changed between read and write
    (e.g. a parallel session editing the same file), returns
    CONCURRENT_MODIFICATION and writes nothing.

    Refuses trashed files (TRASHED_FILE), non-UTF-8 files (NOT_TEXT_FILE),
    unsupported MIME types (UNSUPPORTED_MIME — Google Docs should use
    replace_text/gdoc_batch_replace instead), and files over 5MB
    (FILE_TOO_LARGE)."""
    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(
            fileId=file_id,
            fields="name,mimeType,size,modifiedTime,md5Checksum,trashed,trashedTime",
        )
        .execute()
    )
    if meta.get("trashed"):
        return _trashed_error(file_id, meta)

    try:
        blast_min_delta = int(os.environ.get("BLAST_RADIUS_MIN_DELTA", "200"))
        blast_max_ratio = float(os.environ.get("BLAST_RADIUS_MAX_RATIO", "2"))
        result = await text_ops.apply_edits_to_file(
            drive, file_id, meta,
            edits=[{
                "find": find, "replace": replace,
                "expected_count": expected_count,
                "match_case": match_case, "regex": regex,
            }],
            dry_run=dry_run,
            confirm_delete_chars=confirm_delete_chars,
            blast_min_delta=blast_min_delta,
            blast_max_ratio=blast_max_ratio,
            backup_folder_id=os.environ.get("BACKUP_FOLDER_ID"),
        )
        if "error" not in result:
            result["file_id"] = file_id
            result["file_name"] = meta.get("name", "")
            result["mime_type"] = meta.get("mimeType", "")
        return result
    except HttpError as exc:
        status = exc.resp.status if exc.resp else 0
        return {
            "error": "GOOGLE_API_ERROR",
            "retryable": status in TRANSIENT_CODES,
            "http_status": status,
            "message": (
                f"Google Drive API error (HTTP {status}) after retries: {exc}"
            ),
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_text_replace.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/gsuite_mcp/server.py tests/test_text_replace.py
git commit -m "Add text_replace tool for surgical plain-text Drive file edits"
```

---

### Task 7: `server.py` — `text_batch_replace` tool

**Files:**
- Modify: `src/gsuite_mcp/server.py`
- Create: `tests/test_text_batch_replace.py`

**Interfaces:**
- Consumes: `text_ops.apply_edits_to_file` (Task 5), `_trashed_error` (existing).
- Produces: `@mcp.tool() async def text_batch_replace(file_id, edits, dry_run=False, confirm_delete_chars=None) -> dict`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_text_batch_replace.py`:

```python
"""Tests for the text_batch_replace tool wrapper in server.py."""

from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def mock_drive():
    with patch("gsuite_mcp.auth.get_drive_service") as mock:
        drive = MagicMock()
        mock.return_value = drive
        yield drive


def _live_meta(**overrides):
    base = {
        "name": "strategy.md",
        "mimeType": "text/markdown",
        "size": "40",
        "modifiedTime": "2026-07-10T00:00:00Z",
        "md5Checksum": "abc123",
        "trashed": False,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_empty_edits_rejected(mock_drive):
    from gsuite_mcp.server import text_batch_replace
    result = await text_batch_replace("f1", [])
    assert result["error"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_edit_missing_required_field_rejected(mock_drive):
    from gsuite_mcp.server import text_batch_replace
    result = await text_batch_replace("f1", [{"find": "x"}])
    assert result["error"] == "INVALID_INPUT"
    assert "index 0" in result["message"]


@pytest.mark.asyncio
async def test_refuses_trashed(mock_drive):
    mock_drive.files().get.return_value.execute.return_value = _live_meta(
        trashed=True, trashedTime="2026-07-01T00:00:00Z",
    )
    from gsuite_mcp.server import text_batch_replace
    result = await text_batch_replace("f1", [{"find": "a", "replace": "b"}])
    assert result["error"] == "TRASHED_FILE"


@pytest.mark.asyncio
async def test_seven_edit_batch_applies_atomically(mock_drive):
    content = "\n".join(f"line {i}: old{i}" for i in range(7)).encode()
    mock_drive.files().get.return_value.execute.return_value = _live_meta(size=str(len(content)))
    mock_drive.files().get_media.return_value.execute.return_value = content
    mock_drive.files().update.return_value.execute.return_value = {
        "id": "f1", "name": "strategy.md", "webViewLink": "https://x",
        "version": "2", "modifiedTime": "2026-07-10T01:00:00Z",
    }
    call_count = {"n": 0}
    def revisions_execute():
        call_count["n"] += 1
        return {"revisions": [{"id": f"rev{call_count['n']}"}]}
    revisions_mock = MagicMock()
    revisions_mock.execute = revisions_execute
    mock_drive.revisions().list.return_value = revisions_mock

    edits = [
        {"find": f"old{i}", "replace": f"new{i}", "expected_count": 1}
        for i in range(7)
    ]
    from gsuite_mcp.server import text_batch_replace
    result = await text_batch_replace("f1", edits)
    assert "error" not in result
    assert len(result["per_edit"]) == 7
    assert all(e["applied"] for e in result["per_edit"])
    mock_drive.files().update.assert_called_once()


@pytest.mark.asyncio
async def test_one_failing_edit_aborts_whole_batch_no_write(mock_drive):
    content = b"marker0 marker1 marker1 marker2"
    mock_drive.files().get.return_value.execute.return_value = _live_meta(size=str(len(content)))
    mock_drive.files().get_media.return_value.execute.return_value = content

    edits = [
        {"find": "marker0", "replace": "X", "expected_count": 1},
        {"find": "marker1", "replace": "Y", "expected_count": 1},  # 2 matches -> fails
        {"find": "marker2", "replace": "Z", "expected_count": 1},
    ]
    from gsuite_mcp.server import text_batch_replace
    result = await text_batch_replace("f1", edits)
    assert result["error"] == "BATCH_ABORTED"
    assert result["failed_edit_index"] == 1
    mock_drive.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_returns_per_edit_counts_no_write(mock_drive):
    content = b"alpha beta gamma"
    mock_drive.files().get.return_value.execute.return_value = _live_meta(size=str(len(content)))
    mock_drive.files().get_media.return_value.execute.return_value = content

    edits = [
        {"find": "alpha", "replace": "1", "expected_count": 1},
        {"find": "beta", "replace": "2", "expected_count": 1},
    ]
    from gsuite_mcp.server import text_batch_replace
    result = await text_batch_replace("f1", edits, dry_run=True)
    assert result["dry_run"] is True
    assert len(result["per_edit"]) == 2
    mock_drive.files().update.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_text_batch_replace.py -v`
Expected: FAIL — `ImportError: cannot import name 'text_batch_replace' from 'gsuite_mcp.server'`

- [ ] **Step 3: Add `text_batch_replace` tool**

Add to `src/gsuite_mcp/server.py`, directly after `text_replace`:

```python
@mcp.tool()
async def text_batch_replace(
    file_id: str,
    edits: list[dict[str, Any]],
    dry_run: bool = False,
    confirm_delete_chars: Optional[int] = None,
) -> dict[str, Any]:
    """Apply multiple find/replace edits to a plain-text Drive file in one roundtrip.

    One download, one edit pass, one upload — regardless of edit count. This
    is the tool for multi-edit changes to files too large to safely
    round-trip through upload_file's base64 payload: instead of transmitting
    the whole file, send only the find/replace pairs.

    Each edit: {find: str, replace: str, expected_count?: int, match_case?:
    bool, regex?: bool}. Edits apply sequentially in array order — edit N
    sees the result of edits 1..N-1 (same contract as gdoc_batch_replace).
    All-or-nothing: if any edit's expected_count doesn't match, the entire
    batch aborts before any write; BATCH_ABORTED names the failing edit's
    index (failed_edit_index) and its actual match count.

    dry_run=True returns per-edit match counts without writing.

    Large deletions trip a blast-radius guard (BLAST_RADIUS_EXCEEDED); pass
    confirm_delete_chars=<chars_deleted> from the error to proceed. A
    confirmed edit auto-snapshots a backup copy before writing.

    Detects concurrent edits: if the file changed between read and write,
    returns CONCURRENT_MODIFICATION and writes nothing.

    Refuses trashed files (TRASHED_FILE), non-UTF-8 files (NOT_TEXT_FILE),
    unsupported MIME types (UNSUPPORTED_MIME), and files over 5MB
    (FILE_TOO_LARGE)."""
    if not edits:
        return {
            "error": "INVALID_INPUT",
            "retryable": False,
            "message": "edits array must not be empty.",
        }
    for i, edit in enumerate(edits):
        if "find" not in edit or "replace" not in edit:
            return {
                "error": "INVALID_INPUT",
                "retryable": False,
                "message": (
                    f"Edit at index {i} missing required field(s). "
                    f"Each edit must have 'find' and 'replace'."
                ),
            }

    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(
            fileId=file_id,
            fields="name,mimeType,size,modifiedTime,md5Checksum,trashed,trashedTime",
        )
        .execute()
    )
    if meta.get("trashed"):
        return _trashed_error(file_id, meta)

    try:
        blast_min_delta = int(os.environ.get("BLAST_RADIUS_MIN_DELTA", "200"))
        blast_max_ratio = float(os.environ.get("BLAST_RADIUS_MAX_RATIO", "2"))
        result = await text_ops.apply_edits_to_file(
            drive, file_id, meta, edits=edits,
            dry_run=dry_run,
            confirm_delete_chars=confirm_delete_chars,
            blast_min_delta=blast_min_delta,
            blast_max_ratio=blast_max_ratio,
            backup_folder_id=os.environ.get("BACKUP_FOLDER_ID"),
        )
        if "error" not in result:
            result["file_id"] = file_id
            result["file_name"] = meta.get("name", "")
            result["mime_type"] = meta.get("mimeType", "")
        return result
    except HttpError as exc:
        status = exc.resp.status if exc.resp else 0
        return {
            "error": "GOOGLE_API_ERROR",
            "retryable": status in TRANSIENT_CODES,
            "http_status": status,
            "message": (
                f"Google Drive API error (HTTP {status}) after retries: {exc}"
            ),
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_text_batch_replace.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/gsuite_mcp/server.py tests/test_text_batch_replace.py
git commit -m "Add text_batch_replace tool for atomic multi-edit plain-text file changes"
```

---

### Task 8: `text_ops.read_range` + `server.py` `text_read_range` tool

**Files:**
- Modify: `src/gsuite_mcp/text_ops.py`
- Modify: `src/gsuite_mcp/server.py`
- Create: `tests/test_text_read_range.py`

**Interfaces:**
- Consumes: `pagination.decode_cursor`/`encode_cursor`/`offset_from`/`take_within_budget` (existing, `src/gsuite_mcp/pagination.py`), `drive_ops.download_file_bytes` (Task 2), `is_supported_mime`/`decode_text`/`MAX_FILE_SIZE_BYTES` (Tasks 3-5).
- Produces:
  - `async def read_range(drive_service, file_id, meta, start_line, end_line, max_bytes, cursor) -> dict[str, Any]` in `text_ops.py`.
  - `@mcp.tool() async def text_read_range(file_id, start_line=None, end_line=None, max_bytes=100_000, cursor=None) -> dict` in `server.py`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_text_read_range.py`:

```python
"""Tests for text_ops.read_range and the text_read_range tool wrapper."""

from unittest.mock import patch, MagicMock

import pytest

from gsuite_mcp import text_ops, pagination


def _meta(**overrides):
    base = {"name": "notes.md", "mimeType": "text/markdown", "size": "100"}
    base.update(overrides)
    return base


def _mock_drive(content: bytes):
    svc = MagicMock()
    svc.files().get_media.return_value.execute.return_value = content
    return svc


@pytest.mark.asyncio
async def test_read_range_unsupported_mime():
    svc = _mock_drive(b"content")
    result = await text_ops.read_range(
        svc, "f1", _meta(mimeType="application/pdf"), None, None, 100_000, None,
    )
    assert result["error"] == "UNSUPPORTED_MIME"


@pytest.mark.asyncio
async def test_read_range_full_small_file():
    content = "\n".join(f"line{i}" for i in range(5)).encode()
    svc = _mock_drive(content)
    result = await text_ops.read_range(
        svc, "f1", _meta(size=str(len(content))), None, None, 100_000, None,
    )
    assert result["content"] == content.decode()
    assert result["total_lines"] == 5
    assert result["truncated"] is False
    assert result["next_cursor"] is None


@pytest.mark.asyncio
async def test_read_range_start_end_line_bounds():
    content = "\n".join(f"line{i}" for i in range(10)).encode()
    svc = _mock_drive(content)
    result = await text_ops.read_range(
        svc, "f1", _meta(size=str(len(content))), 2, 4, 100_000, None,
    )
    assert result["content"] == "line2\nline3\nline4"
    assert result["total_lines"] == 10


@pytest.mark.asyncio
async def test_read_range_max_bytes_truncates_and_returns_cursor():
    content = "\n".join(f"line{i}" for i in range(1000)).encode()
    svc = _mock_drive(content)
    result = await text_ops.read_range(
        svc, "f1", _meta(size=str(len(content))), None, None, 50, None,
    )
    assert result["truncated"] is True
    assert result["next_cursor"] is not None
    assert len(result["content"].encode()) <= 50 or result["content"] == "line0"


@pytest.mark.asyncio
async def test_read_range_cursor_continues_from_offset():
    content = "\n".join(f"line{i}" for i in range(10)).encode()
    svc = _mock_drive(content)
    cursor = pagination.encode_cursor({"kind": "text_range", "offset": 5})
    result = await text_ops.read_range(
        svc, "f1", _meta(size=str(len(content))), None, None, 100_000, cursor,
    )
    assert result["content"] == "line5\nline6\nline7\nline8\nline9"
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_read_range_invalid_cursor():
    svc = _mock_drive(b"line0\nline1")
    result = await text_ops.read_range(
        svc, "f1", _meta(size="11"), None, None, 100_000, "not-a-valid-cursor!!",
    )
    assert result["error"] == "INVALID_CURSOR"


@pytest.mark.asyncio
async def test_text_read_range_tool_refuses_trashed():
    with patch("gsuite_mcp.auth.get_drive_service") as mock:
        drive = MagicMock()
        mock.return_value = drive
        drive.files().get.return_value.execute.return_value = {
            "name": "notes.md", "mimeType": "text/markdown", "size": "10",
            "trashed": True, "trashedTime": "2026-07-01T00:00:00Z",
        }
        from gsuite_mcp.server import text_read_range
        result = await text_read_range("f1")
        assert result["error"] == "TRASHED_FILE"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_text_read_range.py -v`
Expected: FAIL — `AttributeError: module 'gsuite_mcp.text_ops' has no attribute 'read_range'`

- [ ] **Step 3: Implement `text_ops.read_range`**

Append to `src/gsuite_mcp/text_ops.py` (needs `from gsuite_mcp import pagination` added to the top-of-file imports alongside the existing `drive_ops` import):

```python
async def read_range(
    drive_service,
    file_id: str,
    meta: dict[str, Any],
    start_line: int | None,
    end_line: int | None,
    max_bytes: int,
    cursor: str | None,
) -> dict[str, Any]:
    """Read a bounded slice of a plain-text Drive file's lines.

    cursor, when present, takes precedence over start_line/end_line and
    continues a prior truncated read. meta must carry mimeType and size from
    a fresh files().get() call by the caller (the caller also performs the
    TRASHED_FILE check before calling this).
    """
    mime_type = meta.get("mimeType", "")
    if not is_supported_mime(mime_type):
        return {
            "error": "UNSUPPORTED_MIME",
            "retryable": False,
            "message": (
                "text_read_range only works on plain-text files (text/*, "
                f"application/json, application/x-yaml). This file is {mime_type}."
            ),
        }

    size = int(meta.get("size") or 0)
    if size > MAX_FILE_SIZE_BYTES:
        return {
            "error": "FILE_TOO_LARGE",
            "retryable": False,
            "size_bytes": size,
            "message": f"File is {size} bytes; text_read_range refuses files over {MAX_FILE_SIZE_BYTES} bytes.",
        }

    raw = await drive_ops.download_file_bytes(drive_service, file_id)
    try:
        decoded = decode_text(raw)
    except UnicodeDecodeError:
        return {
            "error": "NOT_TEXT_FILE",
            "retryable": False,
            "message": "File content is not valid UTF-8 text.",
        }

    lines = decoded["text"].split("\n")
    total_lines = len(lines)

    if cursor is not None:
        try:
            payload = pagination.decode_cursor(cursor)
            start = pagination.offset_from(payload, total_lines)
        except ValueError:
            return {
                "error": "INVALID_CURSOR",
                "retryable": False,
                "message": "Cursor is malformed or unrecognized.",
            }
        hard_end = total_lines
    elif start_line is not None or end_line is not None:
        start = max(0, start_line or 0)
        hard_end = total_lines if end_line is None else min(total_lines, end_line + 1)
    else:
        start = 0
        hard_end = total_lines

    sizes = [len(line.encode("utf-8")) + 1 for line in lines]
    end = pagination.take_within_budget(sizes, start, max_bytes, hard_limit=hard_end - start)
    truncated = end < total_lines
    next_cursor = (
        pagination.encode_cursor({"kind": "text_range", "offset": end})
        if truncated else None
    )

    return {
        "content": "\n".join(lines[start:end]),
        "total_lines": total_lines,
        "truncated": truncated,
        "next_cursor": next_cursor,
        "mime_type": mime_type,
        "line_ending": decoded["line_ending"],
    }
```

Update the top of `src/gsuite_mcp/text_ops.py` imports:

```python
from gsuite_mcp import drive_ops, pagination
```

- [ ] **Step 4: Add the `text_read_range` tool to `server.py`**

Add to `src/gsuite_mcp/server.py`, directly after `text_batch_replace`:

```python
@mcp.tool()
async def text_read_range(
    file_id: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    max_bytes: int = 100_000,
    cursor: Optional[str] = None,
) -> dict[str, Any]:
    """Read a bounded slice of a plain-text Drive file, to build a text_replace find string.

    start_line/end_line select an initial line range (0-indexed, inclusive);
    omit both to start at the top of the file. max_bytes caps the response
    size (default 100000) — follow the returned next_cursor in a follow-up
    call to continue past a truncated response; cursor takes precedence over
    start_line/end_line when both are given.

    Read-only. Same MIME allowlist (text/*, application/json,
    application/x-yaml) and 5MB size ceiling as text_replace."""
    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(fileId=file_id, fields="name,mimeType,size,trashed,trashedTime")
        .execute()
    )
    if meta.get("trashed"):
        return _trashed_error(file_id, meta)
    return await text_ops.read_range(
        drive, file_id, meta, start_line, end_line, max_bytes, cursor,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_text_read_range.py -v`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/gsuite_mcp/text_ops.py src/gsuite_mcp/server.py tests/test_text_read_range.py
git commit -m "Add text_read_range tool for bounded plain-text file reads"
```

---

### Task 9: Trashed-file refusal coverage in the shared test file

**Files:**
- Modify: `tests/test_trashed_refusal.py`

**Interfaces:**
- Consumes: `text_replace`, `text_batch_replace`, `text_read_range` (Tasks 6-8) — this task only adds them to the existing shared trashed-refusal test file for consistency with how every other mutating tool is covered there. (`text_replace`/`text_batch_replace` already have their own dedicated trashed tests from Tasks 6-7; this task centralizes them alongside the rest for discoverability.)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_trashed_refusal.py`, after `test_upload_file_create_allows_no_check`:

```python
@pytest.fixture
def trashed_text_meta():
    """Metadata response for a trashed plain-text file."""
    return {
        "name": "notes.md",
        "mimeType": "text/markdown",
        "size": "20",
        "modifiedTime": "2026-05-19T00:00:00Z",
        "md5Checksum": "abc123",
        "trashed": True,
        "trashedTime": "2026-05-06T18:00:00Z",
    }


@pytest.fixture
def mock_drive_text(trashed_text_meta):
    with patch("gsuite_mcp.auth.get_drive_service") as mock:
        drive = MagicMock()
        mock.return_value = drive
        drive.files().get.return_value.execute.return_value = trashed_text_meta
        yield drive


@pytest.mark.asyncio
async def test_text_replace_refuses_trashed(mock_drive_text):
    from gsuite_mcp.server import text_replace
    result = await text_replace("f1", "old", "new")
    assert result["error"] == "TRASHED_FILE"


@pytest.mark.asyncio
async def test_text_batch_replace_refuses_trashed(mock_drive_text):
    from gsuite_mcp.server import text_batch_replace
    result = await text_batch_replace("f1", [{"find": "old", "replace": "new"}])
    assert result["error"] == "TRASHED_FILE"


@pytest.mark.asyncio
async def test_text_read_range_refuses_trashed(mock_drive_text):
    from gsuite_mcp.server import text_read_range
    result = await text_read_range("f1")
    assert result["error"] == "TRASHED_FILE"
```

- [ ] **Step 2: Run tests to verify they pass** (they should already pass, since Tasks 6-8 implemented the checks — this step confirms no regression, not new behavior)

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest tests/test_trashed_refusal.py -v`
Expected: PASS (all tests, including the 3 new ones)

- [ ] **Step 3: Commit**

```bash
git add tests/test_trashed_refusal.py
git commit -m "Add text_replace/text_batch_replace/text_read_range to shared trashed-refusal tests"
```

---

### Task 10: Update documentation

**Files:**
- Modify: `/Users/josh/Desktop/CODING/gsuite-mcp/CLAUDE.md`

**Interfaces:** None — documentation only.

- [ ] **Step 1: Update the Project Structure section**

In `CLAUDE.md`, add a bullet after the `pagination.py` line:

```markdown
- `src/gsuite_mcp/text_ops.py` — plain-text Drive file editing (matching, UTF-8/line-ending handling, guarded read-match-write core shared by text_replace/text_batch_replace, bounded read_range)
```

- [ ] **Step 2: Update the Tools list**

Renumber isn't required (list is enumerated by position, not by stable ID elsewhere in the file) — append after item 21 (`read_document`):

```markdown
22. `text_replace` — surgical find/replace in a plain-text Drive file (.md/.txt/.csv/.json/.yaml), server-side roundtrip (no base64 payload from caller). `expected_count` checked pre-write, `dry_run`, blast-radius guard + autobackup, optimistic-concurrency check, CRLF-preserving.
23. `text_batch_replace` — atomic multi-edit version of `text_replace`: one download/upload for N sequential find/replace pairs (edit N sees edit N-1's result), all-or-nothing on any `expected_count` mismatch.
24. `text_read_range` — bounded line-range read of a plain-text Drive file, for building `text_replace`/`text_batch_replace` find strings without downloading the whole file.
```

Update the test count and tool count wherever mentioned (search for the exact current count first, since it may have drifted):

Run: `cd ~/Desktop/CODING/gsuite-mcp && uv run pytest -q 2>&1 | tail -5`

Update the `tests/` bullet in Project Structure and the "21 tools" mention in the `server.py` bullet with the actual new counts from that run's summary line and the new tool count (21 + 3 = 24).

- [ ] **Step 3: Add Key Constraints entries**

Append to the Key Constraints section:

```markdown
- `text_replace`/`text_batch_replace`/`text_read_range` operate only on `text/*`, `application/json`, and `application/x-yaml` MIME types — refuse `application/vnd.google-apps.*` with a pointer to `replace_text`/`gdoc_batch_replace`, and refuse any other MIME with `UNSUPPORTED_MIME`. 5MB size ceiling (`FILE_TOO_LARGE` above that). Non-UTF-8 content is refused with `NOT_TEXT_FILE` — no lossy fallback codec.
- `text_replace`/`text_batch_replace` share one core (`text_ops.apply_edits_to_file`) with the Google Docs tools' safety guarantees: `expected_count` checked before any write, blast-radius guard (same env vars as `replace_section`/`gdoc_batch_replace`) with autobackup on a confirmed trip, and an optimistic-concurrency check (`CONCURRENT_MODIFICATION`) comparing `modifiedTime`/`md5Checksum` at read-time vs. immediately before write.
- `text_ops.ALWAYS_BACKUP_ON_WRITE` (currently `True`) makes every `text_replace`/`text_batch_replace` mutation snapshot an autobackup copy before writing, not just confirmed blast-radius trips — pending confirmation that Drive's `revisions()` API gives a reliable rollback point for plain-text files the way it does for Google Docs (see `docs/superpowers/specs/2026-07-11-text-file-editing-design.md` §8).
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "Document text_replace/text_batch_replace/text_read_range tools"
```

---

## Post-implementation (outside this plan's TDD scope)

Per the project's established workflow: after all tasks above are complete and the full suite is green, push to `origin main` and deploy via `./scripts/deploy.sh`. Confirm with the user before deploying, since it touches the live Cloud Run service — this plan's tasks stop at a green local test suite and committed history.
