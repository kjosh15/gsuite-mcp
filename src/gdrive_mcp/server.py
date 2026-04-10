"""Google Drive MCP server with 4 file I/O tools."""

import asyncio
import base64
import io
import logging
import os
import sys
from typing import Any, Optional

from googleapiclient.errors import HttpError
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
    """Upload a file to Google Drive.

    IMPORTANT — Two modes with very different constraints:

    1. UPDATE an existing file (file_id provided): ALWAYS WORKS.
       Use this for the .docx tracked-changes workflow: download_file →
       edit locally → upload_file(file_id=...) to update in place,
       preserving URL, sharing, and version history.

    2. CREATE a new file (file_id omitted): FAILS on personal Google Drive
       with a storageQuotaExceeded error. This is a permanent GCP limitation:
       service accounts have zero Drive storage quota and cannot own files on
       personal Drives. If you need to create a NEW file on a personal Drive,
       STOP — do not retry with different parameters, it will always fail.
       Tell the user to upload the file manually via the Drive web UI instead.
       (Create-new only works inside a Google Workspace Shared Drive.)

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
        try:
            result = await asyncio.to_thread(
                lambda: service.files()
                .create(
                    body=body,
                    media_body=media,
                    fields="id,name,webViewLink,version,modifiedTime",
                )
                .execute()
            )
        except HttpError as e:
            if "storageQuotaExceeded" in str(e):
                return {
                    "error": "STORAGE_QUOTA_UNSUPPORTED",
                    "retryable": False,
                    "message": (
                        "Cannot create new files on personal Google Drive via "
                        "this service account. This is a permanent GCP "
                        "limitation (service accounts have no Drive storage "
                        "quota), NOT a transient error. DO NOT RETRY with "
                        "different parameters, different folders, or different "
                        "MIME types — it will always fail. Workaround: ask the "
                        "user to upload the file manually via the Google Drive "
                        "web UI, OR call upload_file again with an existing "
                        "file_id to UPDATE a file in place (updates always "
                        "work). Creating new files only works inside a Google "
                        "Workspace Shared Drive."
                    ),
                }
            raise

    return {
        "file_id": result["id"],
        "file_name": result["name"],
        "web_view_link": result.get("webViewLink", ""),
        "version": result.get("version", ""),
        "modified_time": result.get("modifiedTime", ""),
    }


@mcp.tool()
async def search_files(
    query: str,
    max_results: int = 10,
) -> dict[str, Any]:
    """Search Google Drive for files. Uses Drive API query syntax.

    Examples:
    - name contains 'Stakeholder'
    - mimeType = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    - 'folder_id' in parents and trashed = false
    """
    service = get_drive_service()

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


@mcp.tool()
async def get_file_metadata(file_id: str) -> dict[str, Any]:
    """Get metadata for a Google Drive file without downloading its content."""
    service = get_drive_service()

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


def main() -> None:
    """Run the MCP server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
        force=True,
    )
    import uvicorn

    app = mcp.http_app(stateless_http=True)
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
