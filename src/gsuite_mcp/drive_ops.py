"""Google Drive v3 operations — pure async functions that accept a service."""

import asyncio
import base64
import hashlib
import io
import os
import re
from typing import Any, Optional

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload


_PARENT_QUERY_RE = re.compile(r"'([^']+)'\s+in\s+parents")


async def download_file(
    service,
    file_id: str,
    export_format: Optional[str] = None,
) -> dict[str, Any]:
    metadata = await asyncio.to_thread(
        lambda: service.files()
        .get(fileId=file_id, fields="name,mimeType,size,trashed,trashedTime")
        .execute()
    )
    if export_format:
        content = await asyncio.to_thread(
            lambda: service.files()
            .export(fileId=file_id, mimeType=export_format)
            .execute()
        )
    else:
        content = await asyncio.to_thread(
            lambda: service.files().get_media(fileId=file_id).execute()
        )
    result = {
        "file_id": file_id,
        "file_name": metadata["name"],
        "mime_type": metadata.get("mimeType", ""),
        "size_bytes": len(content),
        "content_base64": base64.b64encode(content).decode(),
        "trashed": metadata.get("trashed", False),
    }
    if metadata.get("trashedTime"):
        result["trashed_time"] = metadata["trashedTime"]
    return result


async def download_file_bytes(service, file_id: str) -> bytes:
    """Download raw file bytes via get_media.

    Thin wrapper for callers that need raw bytes directly rather than
    download_file's base64-wrapped, metadata-carrying result.
    """
    return await asyncio.to_thread(
        lambda: service.files().get_media(fileId=file_id).execute()
    )


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
            # Any other error (403 permissions, 429 rate limit, 5xx) means the
            # probe itself is unreliable, not that the folder is missing. The
            # probe only refines an empty result's label — it must never turn
            # a previously-successful zero-result search into a hard failure.

    return {"files": [], "status": "empty"}


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


async def get_files_metadata(
    service, file_ids: list[str]
) -> dict[str, Any]:
    """Batch get metadata for N file IDs concurrently."""
    async def one(fid: str) -> dict[str, Any]:
        return await get_file_metadata(service, fid)

    gathered = await asyncio.gather(
        *(one(fid) for fid in file_ids),
        return_exceptions=True,
    )
    results = []
    errors = []
    for fid, outcome in zip(file_ids, gathered):
        if isinstance(outcome, Exception):
            errors.append({"file_id": fid, "error": str(outcome)})
        else:
            results.append(outcome)
    return {"results": results, "errors": errors}


async def list_comments(
    service, file_id: str, include_resolved: bool
) -> dict[str, Any]:
    resp = await asyncio.to_thread(
        lambda: service.comments()
        .list(
            fileId=file_id,
            includeDeleted=False,
            pageSize=100,
            fields=(
                "nextPageToken,comments(id,content,createdTime,author,resolved,anchor,"
                "replies(id,content,createdTime,author))"
            ),
        )
        .execute()
    )
    comments = resp.get("comments", [])
    if not include_resolved:
        comments = [c for c in comments if not c.get("resolved", False)]
    has_more = bool(resp.get("nextPageToken"))
    return {
        "comments": [
            {
                "comment_id": c["id"],
                "content": c.get("content", ""),
                "created_time": c.get("createdTime", ""),
                "author": c.get("author", {}).get("displayName", ""),
                "resolved": c.get("resolved", False),
                "anchor": c.get("anchor"),
                "replies": [
                    {
                        "reply_id": r["id"],
                        "content": r.get("content", ""),
                        "created_time": r.get("createdTime", ""),
                        "author": r.get("author", {}).get("displayName", ""),
                    }
                    for r in c.get("replies", [])
                ],
            }
            for c in comments
        ],
        "has_more": has_more,
    }


async def create_comment(
    service, file_id: str, content: str, anchor_text: Optional[str] = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"content": content}
    # anchor_text currently best-effort: Drive's anchor format is complex;
    # we store it in the comment content if anchor_text is provided but
    # not a full structured anchor. Future v2 could implement structured anchors.
    if anchor_text:
        body["content"] = f"[re: '{anchor_text}'] {content}"
    resp = await asyncio.to_thread(
        lambda: service.comments()
        .create(
            fileId=file_id,
            body=body,
            fields="id,content,createdTime,author",
        )
        .execute()
    )
    return {
        "comment_id": resp["id"],
        "content": resp.get("content", ""),
        "created_time": resp.get("createdTime", ""),
        "author": resp.get("author", {}).get("displayName", ""),
    }


async def reply_to_comment(
    service, file_id: str, comment_id: str, content: str
) -> dict[str, Any]:
    resp = await asyncio.to_thread(
        lambda: service.replies()
        .create(
            fileId=file_id,
            commentId=comment_id,
            body={"content": content},
            fields="id,content,createdTime,author",
        )
        .execute()
    )
    return {
        "reply_id": resp["id"],
        "content": resp.get("content", ""),
        "created_time": resp.get("createdTime", ""),
        "author": resp.get("author", {}).get("displayName", ""),
    }


async def resolve_comment(
    service, file_id: str, comment_id: str
) -> dict[str, Any]:
    # The Drive API resolves comments via a reply with action="resolve",
    # not by PATCHing the comment's resolved field (which is read-only).
    await asyncio.to_thread(
        lambda: service.replies()
        .create(
            fileId=file_id,
            commentId=comment_id,
            body={"action": "resolve"},
            fields="id,action",
        )
        .execute()
    )
    # Fetch the comment to confirm resolved status
    resp = await asyncio.to_thread(
        lambda: service.comments()
        .get(
            fileId=file_id,
            commentId=comment_id,
            fields="id,content,resolved",
        )
        .execute()
    )
    return {
        "comment_id": resp["id"],
        "content": resp.get("content", ""),
        "resolved": resp.get("resolved", False),
    }


async def trash_file(service, file_id: str) -> dict[str, Any]:
    """Move a file to the trash."""
    resp = await asyncio.to_thread(
        lambda: service.files()
        .update(
            fileId=file_id,
            body={"trashed": True},
            fields="id,trashed,trashedTime",
        )
        .execute()
    )
    result: dict[str, Any] = {
        "file_id": resp["id"],
        "trashed": resp.get("trashed", True),
    }
    if resp.get("trashedTime"):
        result["trashed_time"] = resp["trashedTime"]
    return result


async def create_backup_copy(
    service,
    file_id: str,
    backup_folder_id: str | None = None,
) -> dict[str, Any]:
    """Create a backup copy of a file before a destructive edit.

    Returns {backup_file_id, backup_file_name}.
    """
    from datetime import datetime, timezone

    meta = await asyncio.to_thread(
        lambda: service.files()
        .get(fileId=file_id, fields="name,parents")
        .execute()
    )
    name = meta.get("name", "Untitled")
    parents = meta.get("parents", [])
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    backup_name = f"{name}__autobackup_{timestamp}"

    target_parent = backup_folder_id or (parents[0] if parents else None)
    body: dict[str, Any] = {"name": backup_name}
    if target_parent:
        body["parents"] = [target_parent]

    copied = await asyncio.to_thread(
        lambda: service.files()
        .copy(fileId=file_id, body=body, fields="id,name")
        .execute()
    )

    return {
        "backup_file_id": copied["id"],
        "backup_file_name": copied["name"],
    }


async def update_file_metadata(
    service,
    file_id: str,
    name: Optional[str] = None,
    add_parent_id: Optional[str] = None,
    remove_parent_id: Optional[str] = None,
) -> dict[str, Any]:
    """Rename and/or move a file via a single Drive files.update call."""
    params: dict[str, Any] = {
        "fileId": file_id,
        "supportsAllDrives": True,
        "fields": "id,name,parents,mimeType,modifiedTime,trashed",
    }
    if name is not None:
        params["body"] = {"name": name}
    if add_parent_id is not None:
        params["addParents"] = add_parent_id
    if remove_parent_id is not None:
        params["removeParents"] = remove_parent_id

    resp = await asyncio.to_thread(lambda: service.files().update(**params).execute())
    return {
        "file_id": resp["id"],
        "name": resp.get("name", ""),
        "parents": resp.get("parents", []),
        "mime_type": resp.get("mimeType", ""),
        "modified_time": resp.get("modifiedTime", ""),
    }


async def untrash_file(service, file_id: str) -> dict[str, Any]:
    """Restore a file from the trash."""
    resp = await asyncio.to_thread(
        lambda: service.files()
        .update(
            fileId=file_id,
            body={"trashed": False},
            fields="id,trashed",
        )
        .execute()
    )
    return {
        "file_id": resp["id"],
        "trashed": resp.get("trashed", False),
    }
