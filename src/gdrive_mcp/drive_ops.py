"""Google Drive v3 operations — pure async functions that accept a service."""

import asyncio
import base64
import io
from typing import Any, Optional

from googleapiclient.http import MediaIoBaseUpload


async def download_file(
    service,
    file_id: str,
    export_format: Optional[str] = None,
) -> dict[str, Any]:
    metadata = await asyncio.to_thread(
        lambda: service.files()
        .get(fileId=file_id, fields="name,mimeType,size")
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
    return {
        "file_id": file_id,
        "file_name": metadata["name"],
        "mime_type": metadata.get("mimeType", ""),
        "size_bytes": len(content),
        "content_base64": base64.b64encode(content).decode(),
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
    media = MediaIoBaseUpload(
        io.BytesIO(file_bytes), mimetype=mime_type, resumable=True
    )
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
    return {
        "file_id": result["id"],
        "file_name": result["name"],
        "web_view_link": result.get("webViewLink", ""),
        "version": result.get("version", ""),
        "modified_time": result.get("modifiedTime", ""),
    }


async def search_files(service, query: str, max_results: int = 10) -> dict[str, Any]:
    response = await asyncio.to_thread(
        lambda: service.files()
        .list(
            q=query,
            pageSize=max_results,
            fields="files(id,name,mimeType,modifiedTime,webViewLink,parents)",
        )
        .execute()
    )
    return {
        "files": [
            {
                "file_id": f["id"],
                "name": f["name"],
                "mime_type": f.get("mimeType", ""),
                "modified_time": f.get("modifiedTime", ""),
                "web_view_link": f.get("webViewLink", ""),
                "parents": f.get("parents", []),
            }
            for f in response.get("files", [])
        ]
    }


async def get_file_metadata(service, file_id: str) -> dict[str, Any]:
    metadata = await asyncio.to_thread(
        lambda: service.files()
        .get(
            fileId=file_id,
            fields="id,name,mimeType,size,modifiedTime,webViewLink,parents,capabilities",
        )
        .execute()
    )
    return {
        "file_id": metadata["id"],
        "name": metadata["name"],
        "mime_type": metadata.get("mimeType", ""),
        "size_bytes": int(metadata.get("size", 0)),
        "modified_time": metadata.get("modifiedTime", ""),
        "web_view_link": metadata.get("webViewLink", ""),
        "parents": metadata.get("parents", []),
        "capabilities": metadata.get("capabilities", {}),
    }


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
            fields=(
                "comments(id,content,createdTime,author,resolved,anchor,"
                "replies(id,content,createdTime,author))"
            ),
        )
        .execute()
    )
    comments = resp.get("comments", [])
    if not include_resolved:
        comments = [c for c in comments if not c.get("resolved", False)]
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
        ]
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
    # Drive API requires content in the PATCH body even when only resolving
    existing = await asyncio.to_thread(
        lambda: service.comments()
        .get(fileId=file_id, commentId=comment_id, fields="content")
        .execute()
    )
    resp = await asyncio.to_thread(
        lambda: service.comments()
        .update(
            fileId=file_id,
            commentId=comment_id,
            body={"resolved": True, "content": existing["content"]},
            fields="id,content,resolved",
        )
        .execute()
    )
    return {
        "comment_id": resp["id"],
        "content": resp.get("content", ""),
        "resolved": resp.get("resolved", False),
    }
