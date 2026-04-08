"""Google Drive MCP server with 4 file I/O tools."""

import asyncio
import base64
import io
from typing import Any, Optional

from googleapiclient.http import MediaIoBaseUpload

from fastmcp import FastMCP

from gdrive_mcp.drive import get_drive_service

mcp = FastMCP("gdrive-mcp")


@mcp.tool()
async def download_file(
    file_id: str,
    export_format: Optional[str] = None,
) -> dict[str, Any]:
    """Download a file from Google Drive by file ID.

    For native Google formats (Docs, Sheets), use export_format to convert.
    Example: export_format="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    to export a Google Doc as .docx.
    """
    service = get_drive_service()

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


@mcp.tool()
async def upload_file(
    content_base64: str,
    file_name: str,
    mime_type: str,
    file_id: Optional[str] = None,
    parent_folder_id: Optional[str] = None,
) -> dict[str, Any]:
    """Upload a file to Google Drive. If file_id is provided, updates the existing
    file in place (preserving URL, sharing, version history). Otherwise creates new.

    For .docx tracked changes: do NOT set convert — the .docx must stay as .docx.
    """
    service = get_drive_service()
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
