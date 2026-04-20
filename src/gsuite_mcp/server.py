"""Google Workspace MCP server — thin wrappers over *_ops modules."""

import asyncio
import logging
import os
import re
import sys
from typing import Any, Optional

from fastmcp import FastMCP

from gsuite_mcp import auth, docs_ops, docx_edits, drive_ops, gmail_ops, sheets_ops
from gsuite_mcp.api_key_middleware import APIKeyMiddleware

mcp = FastMCP("gsuite-mcp")

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


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


@mcp.tool()
async def append_to_file(
    file_id: str,
    content: str,
    separator: str = "\n",
) -> dict[str, Any]:
    """Append content to a file. Uses native API where possible.

    - Google Docs: Docs API batchUpdate InsertText (preserves formatting)
    - Google Sheets: Sheets API values.append (rows split on newline, cols on comma)
    - Other files: download-concat-upload fallback

    Returns {file_id, file_name, mime_type, bytes_appended, modified_time, mode}.
    """
    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(fileId=file_id, fields="name,mimeType,modifiedTime")
        .execute()
    )
    mime = meta.get("mimeType", "")
    name = meta.get("name", "")

    if mime == GOOGLE_DOC_MIME:
        docs = auth.get_docs_service()
        ops_result = await docs_ops.append_text_to_doc(
            docs, file_id, separator + content
        )
        mode = "docs_native"
        # refresh modifiedTime
        meta2 = await asyncio.to_thread(
            lambda: drive.files()
            .get(fileId=file_id, fields="modifiedTime")
            .execute()
        )
        modified_time = meta2.get("modifiedTime", "")
    elif mime == GOOGLE_SHEET_MIME:
        sheets = auth.get_sheets_service()
        ops_result = await sheets_ops.append_rows(sheets, file_id, content)
        mode = "sheets_native"
        meta2 = await asyncio.to_thread(
            lambda: drive.files()
            .get(fileId=file_id, fields="modifiedTime")
            .execute()
        )
        modified_time = meta2.get("modifiedTime", "")
    else:
        # Plain file: download, concat, upload
        current = await asyncio.to_thread(
            lambda: drive.files().get_media(fileId=file_id).execute()
        )
        to_append = (separator + content).encode("utf-8")
        new_bytes = current + to_append
        import base64 as _b64
        upload_result = await drive_ops.upload_file(
            drive,
            content_base64=_b64.b64encode(new_bytes).decode(),
            file_name=name,
            mime_type=mime,
            file_id=file_id,
        )
        mode = "plain_roundtrip"
        modified_time = upload_result.get("modified_time", "")
        ops_result = {"bytes_appended": len(to_append)}

    return {
        "file_id": file_id,
        "file_name": name,
        "mime_type": mime,
        "bytes_appended": ops_result["bytes_appended"],
        "modified_time": modified_time,
        "mode": mode,
    }


@mcp.tool()
async def replace_text(
    file_id: str,
    find: str,
    replace: str,
    match_case: bool = True,
    regex: bool = False,
) -> dict[str, Any]:
    """Replace text in a Google Doc. Exact match by default; regex optional.

    Only works on Google Docs (mimeType application/vnd.google-apps.document).
    For real .docx files, use docx_suggest_edit instead.
    """
    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(fileId=file_id, fields="name,mimeType,modifiedTime")
        .execute()
    )
    if meta.get("mimeType") != GOOGLE_DOC_MIME:
        return {
            "error": "NOT_A_GOOGLE_DOC",
            "retryable": False,
            "message": (
                f"replace_text only works on Google Docs. This file is "
                f"{meta.get('mimeType')}. For real .docx files, use "
                f"docx_suggest_edit. For other files, download/edit/upload."
            ),
        }
    docs = auth.get_docs_service()

    if regex:
        try:
            count = await docs_ops.replace_regex(
                docs, file_id, find, replace, match_case
            )
        except re.error as e:
            return {
                "error": "INVALID_REGEX",
                "retryable": False,
                "message": f"Invalid regex pattern: {e}",
            }
        meta2 = await asyncio.to_thread(
            lambda: drive.files()
            .get(fileId=file_id, fields="modifiedTime")
            .execute()
        )
        return {
            "file_id": file_id,
            "replacements_made": count,
            "regex_mode": True,
            "modified_time": meta2.get("modifiedTime", ""),
        }

    count = await docs_ops.replace_all_text(docs, file_id, find, replace, match_case)
    meta2 = await asyncio.to_thread(
        lambda: drive.files()
        .get(fileId=file_id, fields="modifiedTime")
        .execute()
    )
    return {
        "file_id": file_id,
        "replacements_made": count,
        "regex_mode": False,
        "modified_time": meta2.get("modifiedTime", ""),
    }


@mcp.tool()
async def manage_comments(
    file_id: str,
    action: str,
    comment_id: Optional[str] = None,
    content: Optional[str] = None,
    anchor_text: Optional[str] = None,
    include_resolved: bool = False,
) -> dict[str, Any]:
    """Manage comments on a Drive file. Actions: list, create, reply, resolve.

    Parameter requirements per action:
    - list: no extra params (include_resolved optional)
    - create: content required (anchor_text optional)
    - reply: comment_id and content required
    - resolve: comment_id required
    """
    drive = auth.get_drive_service()

    if action == "list":
        return await drive_ops.list_comments(drive, file_id, include_resolved)

    if action == "create":
        if not content:
            return {
                "error": "MISSING_PARAM", "retryable": False,
                "message": "action='create' requires 'content'",
            }
        return await drive_ops.create_comment(drive, file_id, content, anchor_text)

    if action == "reply":
        if not comment_id or not content:
            return {
                "error": "MISSING_PARAM", "retryable": False,
                "message": "action='reply' requires 'comment_id' and 'content'",
            }
        return await drive_ops.reply_to_comment(drive, file_id, comment_id, content)

    if action == "resolve":
        if not comment_id:
            return {
                "error": "MISSING_PARAM", "retryable": False,
                "message": "action='resolve' requires 'comment_id'",
            }
        return await drive_ops.resolve_comment(drive, file_id, comment_id)

    return {
        "error": "INVALID_ACTION", "retryable": False,
        "message": f"Unknown action '{action}'. Valid: list, create, reply, resolve.",
    }


@mcp.tool()
async def docx_suggest_edit(
    file_id: str,
    find_text: str,
    replace_text: str,
    author: str = "Claude",
) -> dict[str, Any]:
    """Insert tracked-change revision marks into a .docx file.

    Only works on real .docx files in Drive (mimeType
    application/vnd.openxmlformats-officedocument.wordprocessingml.document).
    For Google Docs, use replace_text. Matches must fit within a single
    paragraph (cross-paragraph is v2).
    """
    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(fileId=file_id, fields="name,mimeType,size")
        .execute()
    )
    if meta.get("mimeType") != DOCX_MIME:
        return {
            "error": "NOT_A_DOCX",
            "retryable": False,
            "message": (
                f"docx_suggest_edit only works on .docx files. This file is "
                f"{meta.get('mimeType')}. Use replace_text for Google Docs."
            ),
        }

    original = await asyncio.to_thread(
        lambda: drive.files().get_media(fileId=file_id).execute()
    )
    try:
        modified = docx_edits.insert_tracked_change(
            original, find_text, replace_text, author
        )
    except docx_edits.NotFoundError as e:
        return {
            "error": "FIND_TEXT_NOT_FOUND",
            "retryable": False,
            "message": str(e),
        }
    except docx_edits.CrossParagraphError as e:
        return {
            "error": "CROSS_PARAGRAPH_MATCH",
            "retryable": False,
            "message": (
                f"{e} Split into per-paragraph edits and call this tool once "
                f"per paragraph."
            ),
        }

    import base64 as _b64
    upload_result = await drive_ops.upload_file(
        drive,
        content_base64=_b64.b64encode(modified).decode(),
        file_name=meta["name"],
        mime_type=DOCX_MIME,
        file_id=file_id,
    )
    return {
        "file_id": file_id,
        "file_name": meta["name"],
        "occurrences_edited": 1,
        "modified_time": upload_result.get("modified_time", ""),
    }


@mcp.tool()
async def create_reply_draft(
    thread_id: str,
    in_reply_to_message_id: str,
    to: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    subject: Optional[str] = None,
    content_type: str = "plain",
) -> dict[str, Any]:
    """Create a Gmail draft replying to a message in a thread.

    Fetches the original message headers to set In-Reply-To and References,
    then creates a draft attached to the given thread. Draft-only — does not
    send. The human reviews and sends from Gmail.

    Args:
        thread_id: Gmail thread ID.
        in_reply_to_message_id: Gmail message ID being replied to.
        to: Recipient email address.
        body: Draft body (plain text or HTML).
        cc: Optional CC recipients.
        bcc: Optional BCC recipients.
        subject: Override auto-generated 'Re: <original subject>'.
        content_type: 'plain' (default) or 'html'.
    """
    return await gmail_ops.create_reply_draft(
        gmail_service=auth.get_gmail_service(),
        thread_id=thread_id,
        in_reply_to_message_id=in_reply_to_message_id,
        to=to,
        body=body,
        cc=cc,
        bcc=bcc,
        subject=subject,
        content_type=content_type,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
        force=True,
    )
    api_key = os.environ.get("GSUITE_MCP_API_KEY") or os.environ.get(
        "GDRIVE_MCP_API_KEY"
    )
    if not api_key:
        print(
            "ERROR: GSUITE_MCP_API_KEY (or GDRIVE_MCP_API_KEY) environment "
            "variable is required. Refusing to start an unauthenticated "
            "MCP server.",
            file=sys.stderr,
        )
        sys.exit(1)

    import uvicorn

    app = mcp.http_app(stateless_http=True)
    app.add_middleware(APIKeyMiddleware, api_key=api_key)
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
