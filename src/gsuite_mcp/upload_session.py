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

# On Cloud Run, /tmp (where the session's temp file lands, via tempfile.mkstemp)
# is backed by instance memory (tmpfs), not disk. An unbounded total_bytes lets
# a caller (buggy or malicious) exhaust the container's RAM. 25MB — larger than
# text_ops.MAX_FILE_SIZE_BYTES (5MB) since chunked upload exists specifically
# for payloads bigger than what text_replace's ceiling allows, but still bounded.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

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
    if total_bytes > MAX_UPLOAD_BYTES:
        # Defense-in-depth: server.py's upload_file_start checks this same
        # ceiling before calling in (so it can return a FILE_TOO_LARGE tool
        # error), but this module is also usable directly, so it must not
        # rely solely on the caller doing that check.
        raise ValueError(
            f"total_bytes ({total_bytes}) exceeds MAX_UPLOAD_BYTES "
            f"({MAX_UPLOAD_BYTES})."
        )
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
    _purge_expired()
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
    _purge_expired()
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
