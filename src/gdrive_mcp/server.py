"""Google Drive MCP server — thin wrappers over *_ops modules."""

import logging
import os
import sys
from typing import Any, Optional

from fastmcp import FastMCP

from gdrive_mcp import auth, drive_ops

mcp = FastMCP("gdrive-mcp")


@mcp.tool()
async def download_file(
    file_id: str,
    export_format: Optional[str] = None,
) -> dict[str, Any]:
    """Download a file from Google Drive by file ID.

    For native Google formats (Docs, Sheets), use export_format to convert.
    """
    return await drive_ops.download_file(
        auth.get_drive_service(), file_id, export_format
    )


@mcp.tool()
async def upload_file(
    content_base64: str,
    file_name: str,
    mime_type: str,
    file_id: Optional[str] = None,
    parent_folder_id: Optional[str] = None,
) -> dict[str, Any]:
    """Upload a file to Google Drive (create or update)."""
    return await drive_ops.upload_file(
        auth.get_drive_service(),
        content_base64,
        file_name,
        mime_type,
        file_id,
        parent_folder_id,
    )


@mcp.tool()
async def search_files(query: str, max_results: int = 10) -> dict[str, Any]:
    """Search Google Drive for files. Uses Drive API query syntax."""
    return await drive_ops.search_files(auth.get_drive_service(), query, max_results)


@mcp.tool()
async def get_file_metadata(file_id: str) -> dict[str, Any]:
    """Get metadata for a Google Drive file without downloading its content."""
    return await drive_ops.get_file_metadata(auth.get_drive_service(), file_id)


@mcp.tool()
async def get_files_metadata(file_ids: list[str]) -> dict[str, Any]:
    """Batch get metadata for multiple file IDs concurrently.

    Returns {results: [...], errors: [{file_id, error}]}. Partial failures
    do not abort the whole batch — failed IDs appear in errors.
    """
    return await drive_ops.get_files_metadata(auth.get_drive_service(), file_ids)


def main() -> None:
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
