"""Plain-text Drive file editing utilities — MIME detection, line-ending normalization, and UTF-8 encode/decode."""

import re
from typing import Any

MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024

ALLOWED_EXACT_MIME_TYPES: set[str] = {"application/json", "application/x-yaml"}
GOOGLE_APPS_MIME_PREFIX = "application/vnd.google-apps."


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
