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
