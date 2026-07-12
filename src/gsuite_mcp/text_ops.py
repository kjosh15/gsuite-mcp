"""Plain-text Drive file editing utilities — MIME detection, line-ending normalization, and UTF-8 encode/decode."""

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
