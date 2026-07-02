"""Shared pagination + truncation helpers for bounded reads.

Cursors are opaque base64url(JSON) tokens carrying a version and per-tool
position fields. Callers never parse them. Byte-budget windowing always
emits at least one unit so pagination cannot stall.
"""

import base64
import json
from typing import Any

CURSOR_VERSION = 1


def encode_cursor(payload: dict[str, Any]) -> str:
    """Serialize a cursor payload to an opaque base64url token."""
    raw = json.dumps({"v": CURSOR_VERSION, **payload}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> dict[str, Any]:
    """Decode an opaque cursor token. Raises ValueError if malformed."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed cursor: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise ValueError("unsupported cursor version")
    return payload


def offset_from(payload: dict[str, Any], unit_count: int) -> int:
    """Extract and validate a paginated offset from a decoded cursor payload.

    Raises ValueError if ``offset`` is missing-then-nonzero-invalid, not a
    non-bool int, or outside ``[0, unit_count]`` (``unit_count`` itself is a
    valid empty-tail cursor). Callers map the ValueError to INVALID_CURSOR.
    """
    offset = payload.get("offset", 0)
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise ValueError(f"invalid cursor offset: {offset!r}")
    if offset < 0 or offset > unit_count:
        raise ValueError(f"cursor offset out of range: {offset}")
    return offset


def take_within_budget(
    sizes: list[int],
    start: int,
    max_bytes: int,
    hard_limit: int | None = None,
) -> int:
    """Return the exclusive end index of units ``[start:end]`` fitting max_bytes.

    Always includes at least one unit (``sizes[start]``) when ``start`` is in
    range, guaranteeing forward progress even when a single unit exceeds the
    budget. ``hard_limit`` optionally caps the number of units in the window.
    """
    n = len(sizes)
    if start >= n:
        return start
    end = start
    total = 0
    while end < n:
        if end > start and hard_limit is not None and (end - start) >= hard_limit:
            break
        nxt = total + sizes[end]
        if end > start and nxt > max_bytes:
            break
        total = nxt
        end += 1
    return end
