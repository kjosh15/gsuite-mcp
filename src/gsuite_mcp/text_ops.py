"""Plain-text Drive file editing — matching, decoding, the guarded read-match-write core, and bounded reads."""

import asyncio
import base64
import re
from typing import Any

from gsuite_mcp import drive_ops, pagination
from gsuite_mcp.docs_ops import check_blast_radius

MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024

ALLOWED_EXACT_MIME_TYPES: set[str] = {"application/json", "application/x-yaml"}
GOOGLE_APPS_MIME_PREFIX = "application/vnd.google-apps."

# Safe default from the Task 1 design spike: every write through
# apply_edits_to_file creates a backup copy first, not just confirmed
# blast-radius trips. No live-credential testing was available to validate
# a narrower policy, so this ships conservative.
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
        idx += len(find) or 1
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

    # Optimistic-concurrency check: re-fetch immediately before writing, and
    # before creating any backup, so an aborted write doesn't orphan a
    # backup copy that was never needed.
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

    backup_file_id = None
    backup_file_name = None
    if confirm_delete_chars is not None or ALWAYS_BACKUP_ON_WRITE:
        backup = await drive_ops.create_backup_copy(
            drive_service, file_id, backup_folder_id=backup_folder_id,
        )
        backup_file_id = backup["backup_file_id"]
        backup_file_name = backup["backup_file_name"]

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

    # Best-effort informational lookup: the write has already landed, so a
    # failure here must never surface as an error for the caller — fall
    # back to None rather than let the exception propagate and turn a
    # successful write into a reported failure.
    try:
        rev_resp_after = await asyncio.to_thread(
            lambda: drive_service.revisions()
            .list(fileId=file_id, fields="revisions(id)", pageSize=1000)
            .execute()
        )
        revisions_after = rev_resp_after.get("revisions", [])
        revision_id_after = revisions_after[-1]["id"] if revisions_after else None
    except Exception:
        revision_id_after = None

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
    truncated = end < hard_end
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
