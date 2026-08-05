"""Google Workspace MCP server — thin wrappers over *_ops modules."""

import asyncio
import logging
import os
import re
import sys
from typing import Any, Optional

from fastmcp import FastMCP
from googleapiclient.errors import HttpError

from gsuite_mcp import auth, docs_ops, docx_edits, drive_ops, gdoc_ops, gmail_ops, sheets_ops, text_ops
from gsuite_mcp.retry import TRANSIENT_CODES
from gsuite_mcp.api_key_middleware import APIKeyMiddleware

mcp = FastMCP("gsuite-mcp")

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _trashed_error(file_id: str, meta: dict) -> dict[str, Any]:
    """Return a structured TRASHED_FILE error dict."""
    return {
        "error": "TRASHED_FILE",
        "file_id": file_id,
        "file_name": meta.get("name", ""),
        "trashed_time": meta.get("trashedTime", ""),
        "retryable": False,
        "message": (
            "Cannot modify trashed file. Restore via Drive UI "
            "or call untrash_file first, or use a different file ID."
        ),
    }


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
    if file_id:
        drive = auth.get_drive_service()
        meta = await asyncio.to_thread(
            lambda: drive.files()
            .get(fileId=file_id, fields="name,trashed,trashedTime")
            .execute()
        )
        if meta.get("trashed"):
            return _trashed_error(file_id, meta)
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
    """Get metadata for a Google Drive file without downloading its content.

    Returns trashed: true with trashed_time if the file is in Drive trash."""
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
    Refuses trashed files with error: TRASHED_FILE."""
    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(fileId=file_id, fields="name,mimeType,modifiedTime,trashed,trashedTime")
        .execute()
    )
    if meta.get("trashed"):
        return _trashed_error(file_id, meta)
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
        current = await drive_ops.download_file_bytes(drive, file_id)
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
    expected_count: Optional[int] = None,
    preceded_by: Optional[str] = None,
    followed_by: Optional[str] = None,
) -> dict[str, Any]:
    """Replace text in a Google Doc. Exact match by default; regex optional.

    Only works on Google Docs (mimeType application/vnd.google-apps.document).
    For real .docx files, use docx_suggest_edit instead.

    Note: Operates on paragraph-level text. Patterns spanning paragraph breaks
    will not match — Google Docs treats paragraph breaks as structural objects,
    not newline characters. To operate across paragraph boundaries, use
    format_document with 'delete' action, or chain multiple replace_text calls.

    When expected_count is set, fetches the document first and counts
    occurrences client-side. If the count doesn't match, returns a
    COUNT_MISMATCH error and no mutation occurs.

    When preceded_by and/or followed_by are set, uses client-side matching
    to filter occurrences by surrounding context (within 200 chars).
    Only matching occurrences are replaced. Composes with regex and
    expected_count (count checked after context filtering).

    Refuses trashed files with error: TRASHED_FILE."""
    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(fileId=file_id, fields="name,mimeType,modifiedTime,trashed,trashedTime")
        .execute()
    )
    if meta.get("trashed"):
        return _trashed_error(file_id, meta)
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

    try:
        # Context-anchored mode: use client-side approach
        if preceded_by is not None or followed_by is not None:
            result = await docs_ops.replace_in_context(
                docs, file_id, find, replace, match_case,
                preceded_by=preceded_by, followed_by=followed_by,
                regex=regex, expected_count=expected_count,
            )
            if isinstance(result, dict) and "error" in result:
                return result
            count = result
            meta2 = await asyncio.to_thread(
                lambda: drive.files()
                .get(fileId=file_id, fields="modifiedTime")
                .execute()
            )
            return {
                "file_id": file_id,
                "replacements_made": count,
                "regex_mode": regex,
                "context_filtered": True,
                "modified_time": meta2.get("modifiedTime", ""),
            }

        if regex:
            try:
                result = await docs_ops.replace_regex(
                    docs, file_id, find, replace, match_case,
                    expected_count=expected_count,
                )
            except re.error as e:
                return {
                    "error": "INVALID_REGEX",
                    "retryable": False,
                    "message": f"Invalid regex pattern: {e}",
                }
            if isinstance(result, dict) and "error" in result:
                return result
            count = result
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

        result = await docs_ops.replace_all_text(
            docs, file_id, find, replace, match_case,
            expected_count=expected_count,
        )
        if isinstance(result, dict) and "error" in result:
            return result
        count = result
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
    except HttpError as exc:
        status = exc.resp.status if exc.resp else 0
        return {
            "error": "GOOGLE_API_ERROR",
            "retryable": status in TRANSIENT_CODES,
            "http_status": status,
            "message": (
                f"Google Docs API error (HTTP {status}) after retries: {exc}"
            ),
        }


@mcp.tool()
async def replace_section(
    file_id: str,
    section_heading: str,
    new_content: str,
    include_heading: bool = False,
    dry_run: bool = False,
    expected_delete_chars: Optional[int] = None,
    confirm_delete_chars: Optional[int] = None,
) -> dict[str, Any]:
    """Replace content in a Google Doc by heading/section.

    Finds a heading by text match (formal heading styles first, then any
    paragraph as fallback), determines the section boundary (to the next
    same-or-higher-level heading), and replaces the content atomically.

    Args:
        file_id: Google Drive file ID of a native Google Doc.
        section_heading: Text of the heading to find (case-insensitive, stripped).
        new_content: Replacement text for the section body (or heading+body
            if include_heading=True).
        include_heading: If True, also replace the heading paragraph itself.
            Default False (preserve heading, replace only body).
        dry_run: If True, compute the span and char counts without mutating.
        expected_delete_chars: If set, abort with DELETE_CHARS_MISMATCH when
            the computed deletion size differs.
        confirm_delete_chars: Pass the exact chars_deleted value from a prior
            dry_run or BLAST_RADIUS_EXCEEDED error to bypass the blast-radius
            guard. When provided, an auto-backup snapshot is created before
            the mutation.

    Refuses trashed files with error: TRASHED_FILE."""
    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(fileId=file_id, fields="name,mimeType,modifiedTime,trashed,trashedTime")
        .execute()
    )
    if meta.get("trashed"):
        return _trashed_error(file_id, meta)
    if meta.get("mimeType") != GOOGLE_DOC_MIME:
        return {
            "error": "NOT_A_GOOGLE_DOC",
            "retryable": False,
            "message": (
                f"replace_section only works on Google Docs. This file is "
                f"{meta.get('mimeType')}. For real .docx files, use "
                f"docx_suggest_edit. For other files, download/edit/upload."
            ),
        }
    docs = auth.get_docs_service()

    try:
        # Auto-snapshot: create backup before mutation when caller confirmed blast-radius
        backup_info = None
        if confirm_delete_chars is not None:
            backup_info = await drive_ops.create_backup_copy(
                drive, file_id,
                backup_folder_id=os.environ.get("BACKUP_FOLDER_ID"),
            )

        # Read blast-radius thresholds from env
        blast_min_delta = int(os.environ.get("BLAST_RADIUS_MIN_DELTA", "200"))
        blast_max_ratio = float(os.environ.get("BLAST_RADIUS_MAX_RATIO", "2"))

        result = await docs_ops.replace_section(
            docs, file_id, section_heading, new_content, include_heading,
            dry_run=dry_run,
            expected_delete_chars=expected_delete_chars,
            confirm_delete_chars=confirm_delete_chars,
            blast_min_delta=blast_min_delta,
            blast_max_ratio=blast_max_ratio,
        )
        if "error" in result:
            return result

        # Merge backup info into successful result
        if backup_info and "error" not in result:
            result["backup_file_id"] = backup_info["backup_file_id"]
            result["backup_file_name"] = backup_info["backup_file_name"]

        if not dry_run:
            # Fetch updated modifiedTime
            meta2 = await asyncio.to_thread(
                lambda: drive.files()
                .get(fileId=file_id, fields="modifiedTime")
                .execute()
            )
            result["modified_time"] = meta2.get("modifiedTime", "")
        return result
    except HttpError as exc:
        status = exc.resp.status if exc.resp else 0
        return {
            "error": "GOOGLE_API_ERROR",
            "retryable": status in TRANSIENT_CODES,
            "http_status": status,
            "message": (
                f"Google Docs API error (HTTP {status}) after retries: {exc}"
            ),
        }


@mcp.tool()
async def format_document(
    file_id: str,
    operations: list[dict[str, Any]],
    preview: bool = False,
) -> dict[str, Any]:
    """Apply paragraph formatting operations to a Google Doc in a single batch.

    Each operation is a dict with an "action" key:

    - set_style: Change paragraph style.
      {"action": "set_style", "find_text": "Introduction", "style": "HEADING_1"}
      Valid styles: NORMAL_TEXT, TITLE, SUBTITLE, HEADING_1..HEADING_6.

    - set_text_style: Apply inline text formatting to a paragraph.
      {"action": "set_text_style", "find_text": "Introduction",
       "style": {"italic": true, "bold": true}}
      Valid style keys: bold, italic, underline, strikethrough.
      Only specified keys are changed; omitted keys left unchanged.

    - delete: Delete a paragraph.
      {"action": "delete", "find_text": "Paragraph to remove"}

    - delete_by_index: Delete a paragraph by its content index (from a prior read).
      {"action": "delete_by_index", "paragraph_index": 3}

    - delete_empty_after: Remove blank paragraphs after a matched paragraph.
      {"action": "delete_empty_after", "find_text": "Introduction"}

    - insert_paragraph: Insert a new paragraph after a content block index.
      {"action": "insert_paragraph", "after_paragraph_index": 3,
       "text": "New item", "text_style": {"italic": true}}
      Inherits list formatting from neighbor by default.
      Optional overrides: nesting_level, list_id, text_style.

    - insert_paragraph_after_match: Find a paragraph by text, insert after it.
      {"action": "insert_paragraph_after_match", "find_text": "Existing item",
       "text": "New item", "inherit_list_formatting": true}
      Multi-match returns error (no match_all support).
      Optional: inherit_list_formatting, nesting_level, list_id, text_style.

    Matching rules:
    - find_text matching is exact (strip + case-fold) by default.
    - Add "substring": true on an operation for substring matching.
    - Add "match_mode": "regex" for regex matching (case-sensitive; use (?i) for
      case-insensitive). Invalid patterns return INVALID_REGEX error.
    - "match_mode" takes precedence over "substring" flag. Valid values:
      "exact" (default), "substring", "regex".
    - If a delete or set_style matches multiple paragraphs, it fails with
      a multi_match_error listing all matches (paragraph index + text snippet).
      Pass "match_all": true on the operation to apply to all matches.

    Top-level options:
    - preview: If true, returns what each operation would affect (paragraph
      index + first 80 chars + action) without executing any changes.

    Only works on Google Docs (mimeType application/vnd.google-apps.document).
    Refuses trashed files with error: TRASHED_FILE."""
    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(fileId=file_id, fields="name,mimeType,modifiedTime,trashed,trashedTime")
        .execute()
    )
    if meta.get("trashed"):
        return _trashed_error(file_id, meta)
    if meta.get("mimeType") != GOOGLE_DOC_MIME:
        return {
            "error": "NOT_A_GOOGLE_DOC",
            "retryable": False,
            "message": (
                f"format_document only works on Google Docs. This file is "
                f"{meta.get('mimeType')}."
            ),
        }
    docs = auth.get_docs_service()

    try:
        result = await docs_ops.format_document(docs, file_id, operations, preview=preview)
        if "error" in result:
            return result
        if not preview:
            meta2 = await asyncio.to_thread(
                lambda: drive.files()
                .get(fileId=file_id, fields="modifiedTime")
                .execute()
            )
            result["modified_time"] = meta2.get("modifiedTime", "")
        return result
    except HttpError as exc:
        status = exc.resp.status if exc.resp else 0
        return {
            "error": "GOOGLE_API_ERROR",
            "retryable": status in TRANSIENT_CODES,
            "http_status": status,
            "message": (
                f"Google Docs API error (HTTP {status}) after retries: {exc}"
            ),
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

    if action in ("create", "reply", "resolve"):
        meta = await asyncio.to_thread(
            lambda: drive.files()
            .get(fileId=file_id, fields="name,trashed,trashedTime")
            .execute()
        )
        if meta.get("trashed"):
            return _trashed_error(file_id, meta)

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
        .get(fileId=file_id, fields="name,mimeType,size,trashed,trashedTime")
        .execute()
    )
    if meta.get("trashed"):
        return _trashed_error(file_id, meta)
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
async def gdoc_template_populate(
    template_file_id: str,
    parent_folder_id: str,
    new_title: str,
    replacements: dict[str, str],
    post_styles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Copy a template file as a native Google Doc and replace placeholders.

    Copies the template using Drive files.copy with automatic .docx-to-Google-Doc
    conversion, places it in the specified parent folder, then issues a single
    documents.batchUpdate with replaceAllText for each placeholder.

    Optionally applies paragraph formatting operations (same schema as
    format_document) after placeholder replacement via post_styles.

    Returns {file_id, web_view_link, replacements_made: {placeholder: count}}.
    """
    try:
        return await gdoc_ops.template_populate(
            drive_service=auth.get_drive_service(),
            docs_service=auth.get_docs_service(),
            template_file_id=template_file_id,
            parent_folder_id=parent_folder_id,
            new_title=new_title,
            replacements=replacements,
            post_styles=post_styles,
        )
    except HttpError as exc:
        status = exc.resp.status if exc.resp else 0
        return {
            "error": "GOOGLE_API_ERROR",
            "retryable": status in TRANSIENT_CODES,
            "http_status": status,
            "message": (
                f"Google API error (HTTP {status}) during template populate: {exc}"
            ),
        }


@mcp.tool()
async def gdoc_suggest_edit(
    file_id: str,
    find_text: str,
    replace_text: str,
    author: str = "Claude",
) -> dict[str, Any]:
    """Create a .docx copy of a Google Doc with tracked-change suggestions.

    Exports the Google Doc as .docx, applies tracked-change revision marks
    for find_text -> replace_text, and uploads the result as a new .docx file
    in the same folder. The original Google Doc is unchanged.

    Open the new .docx in Google Docs or Word to review suggestions.
    For .docx files already in Drive, use docx_suggest_edit instead.
    """
    try:
        return await gdoc_ops.suggest_edit(
            drive_service=auth.get_drive_service(),
            file_id=file_id,
            find_text=find_text,
            replace_text=replace_text,
            author=author,
        )
    except HttpError as exc:
        status = exc.resp.status if exc.resp else 0
        return {
            "error": "GOOGLE_API_ERROR",
            "retryable": status in TRANSIENT_CODES,
            "http_status": status,
            "message": (
                f"Google API error (HTTP {status}) during suggest edit: {exc}"
            ),
        }


@mcp.tool()
async def gdoc_batch_replace(
    file_id: str,
    edits: list[dict[str, Any]],
    dry_run: bool = False,
    allow_review_docs: bool = False,
    confirm_delete_chars: Optional[int] = None,
) -> dict[str, Any]:
    """Batch find/replace in a live Google Doc, in place.

    Accepts an array of find/replace pairs applied atomically in one
    batchUpdate. Supports cross-paragraph matches. Preserves file ID.

    Each edit: {find_text: str, replace_text: str, expected_count?: int}.
    If any pair's expected_count doesn't match, the entire batch aborts.

    dry_run=True returns per-pair match counts without writing.
    allow_review_docs=False (default) blocks edits to hand-review docs
    listed in the GDOC_REVIEW_DOC_IDS env var.

    confirm_delete_chars: Pass the exact chars_deleted value from a prior
    dry_run or BLAST_RADIUS_EXCEEDED error to bypass the blast-radius
    guard. When provided, an auto-backup snapshot is created before
    the mutation.

    Returns revision_id_before/after (Drive revision IDs) for rollback.
    Refuses trashed files with error: TRASHED_FILE."""
    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(fileId=file_id, fields="name,mimeType,modifiedTime,trashed,trashedTime")
        .execute()
    )
    if meta.get("trashed"):
        return _trashed_error(file_id, meta)
    if meta.get("mimeType") != GOOGLE_DOC_MIME:
        return {
            "error": "NOT_A_GOOGLE_DOC",
            "retryable": False,
            "message": (
                f"gdoc_batch_replace only works on Google Docs. This file is "
                f"{meta.get('mimeType')}."
            ),
        }

    # Denylist guard — fail-closed: refuse if env var is unset or empty
    review_ids_raw = os.environ.get("GDOC_REVIEW_DOC_IDS", "")
    review_ids = {rid.strip() for rid in review_ids_raw.split(",") if rid.strip()}
    if not review_ids:
        return {
            "error": "DENYLIST_NOT_CONFIGURED",
            "retryable": False,
            "message": (
                "GDOC_REVIEW_DOC_IDS env var is not set or empty. "
                "gdoc_batch_replace refuses to run without an explicit "
                "denylist. Set the var to a comma-separated list of "
                "file IDs that require hand-review."
            ),
        }
    if file_id in review_ids and not allow_review_docs:
        return {
            "error": "REVIEW_DOC_BLOCKED",
            "retryable": False,
            "file_id": file_id,
            "file_name": meta.get("name", ""),
            "message": (
                "This document is in the hand-review denylist "
                "(GDOC_REVIEW_DOC_IDS). Use gdoc_suggest_edit for review "
                "docs, or pass allow_review_docs=True to override."
            ),
        }

    # Validate edits
    if not edits:
        return {
            "error": "INVALID_INPUT",
            "retryable": False,
            "message": "edits array must not be empty.",
        }
    for i, edit in enumerate(edits):
        if "find_text" not in edit or "replace_text" not in edit:
            return {
                "error": "INVALID_INPUT",
                "retryable": False,
                "message": (
                    f"Edit at index {i} missing required field(s). "
                    f"Each edit must have 'find_text' and 'replace_text'."
                ),
            }

    docs = auth.get_docs_service()
    try:
        # Auto-snapshot: create backup before mutation when caller confirmed blast-radius
        backup_info = None
        if confirm_delete_chars is not None:
            backup_info = await drive_ops.create_backup_copy(
                drive, file_id,
                backup_folder_id=os.environ.get("BACKUP_FOLDER_ID"),
            )

        # Read blast-radius thresholds from env
        blast_min_delta = int(os.environ.get("BLAST_RADIUS_MIN_DELTA", "200"))
        blast_max_ratio = float(os.environ.get("BLAST_RADIUS_MAX_RATIO", "2"))

        result = await gdoc_ops.batch_replace(
            drive, docs, file_id, edits, dry_run=dry_run,
            confirm_delete_chars=confirm_delete_chars,
            blast_min_delta=blast_min_delta,
            blast_max_ratio=blast_max_ratio,
        )
        result["file_id"] = file_id

        # Merge backup info into successful result
        if backup_info and "error" not in result:
            result["backup_file_id"] = backup_info["backup_file_id"]
            result["backup_file_name"] = backup_info["backup_file_name"]

        if result.get("committed"):
            meta2 = await asyncio.to_thread(
                lambda: drive.files()
                .get(fileId=file_id, fields="modifiedTime")
                .execute()
            )
            result["modified_time"] = meta2.get("modifiedTime", "")
        return result
    except HttpError as exc:
        status = exc.resp.status if exc.resp else 0
        return {
            "error": "GOOGLE_API_ERROR",
            "retryable": status in TRANSIENT_CODES,
            "http_status": status,
            "message": (
                f"Google Docs API error (HTTP {status}) after retries: {exc}"
            ),
        }


@mcp.tool()
async def text_replace(
    file_id: str,
    find: str,
    replace: str,
    expected_count: Optional[int] = None,
    match_case: bool = True,
    regex: bool = False,
    dry_run: bool = False,
    confirm_delete_chars: Optional[int] = None,
) -> dict[str, Any]:
    """Surgical find/replace in a plain-text Drive file (.md, .txt, .csv, .json, .yaml).

    Downloads, edits, and re-uploads server-side — the caller never needs to
    transmit the file's full contents. Matching operates on the raw text
    stream: newlines are ordinary characters, so multi-line find patterns
    work naturally (unlike replace_text on Google Docs, which is
    paragraph-bound).

    expected_count is checked before any write: on mismatch, returns
    COUNT_MISMATCH (or NO_MATCH if zero matches) with the actual count, and
    writes nothing. dry_run=True returns matches_found without writing.

    Large deletions trip a blast-radius guard (BLAST_RADIUS_EXCEEDED); pass
    confirm_delete_chars=<chars_deleted> from the error to proceed. A
    confirmed edit auto-snapshots a backup copy before writing.

    Detects concurrent edits: if the file changed between read and write
    (e.g. a parallel session editing the same file), returns
    CONCURRENT_MODIFICATION and writes nothing.

    Refuses trashed files (TRASHED_FILE), non-UTF-8 files (NOT_TEXT_FILE),
    unsupported MIME types (UNSUPPORTED_MIME — Google Docs should use
    replace_text/gdoc_batch_replace instead), and files over 5MB
    (FILE_TOO_LARGE)."""
    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(
            fileId=file_id,
            fields="name,mimeType,size,modifiedTime,md5Checksum,trashed,trashedTime",
        )
        .execute()
    )
    if meta.get("trashed"):
        return _trashed_error(file_id, meta)

    try:
        blast_min_delta = int(os.environ.get("BLAST_RADIUS_MIN_DELTA", "200"))
        blast_max_ratio = float(os.environ.get("BLAST_RADIUS_MAX_RATIO", "2"))
        result = await text_ops.apply_edits_to_file(
            drive, file_id, meta,
            edits=[{
                "find": find, "replace": replace,
                "expected_count": expected_count,
                "match_case": match_case, "regex": regex,
            }],
            dry_run=dry_run,
            confirm_delete_chars=confirm_delete_chars,
            blast_min_delta=blast_min_delta,
            blast_max_ratio=blast_max_ratio,
            backup_folder_id=os.environ.get("BACKUP_FOLDER_ID"),
        )
        if "error" not in result:
            result["file_id"] = file_id
            result["file_name"] = meta.get("name", "")
            result["mime_type"] = meta.get("mimeType", "")
        return result
    except HttpError as exc:
        status = exc.resp.status if exc.resp else 0
        return {
            "error": "GOOGLE_API_ERROR",
            "retryable": status in TRANSIENT_CODES,
            "http_status": status,
            "message": (
                f"Google Drive API error (HTTP {status}) after retries: {exc}"
            ),
        }


@mcp.tool()
async def text_batch_replace(
    file_id: str,
    edits: list[dict[str, Any]],
    dry_run: bool = False,
    confirm_delete_chars: Optional[int] = None,
) -> dict[str, Any]:
    """Apply multiple find/replace edits to a plain-text Drive file in one roundtrip.

    One download, one edit pass, one upload — regardless of edit count. This
    is the tool for multi-edit changes to files too large to safely
    round-trip through upload_file's base64 payload: instead of transmitting
    the whole file, send only the find/replace pairs.

    Each edit: {find: str, replace: str, expected_count?: int, match_case?:
    bool, regex?: bool}. Edits apply sequentially in array order — edit N
    sees the result of edits 1..N-1 (same contract as gdoc_batch_replace).
    All-or-nothing: if any edit's expected_count doesn't match, the entire
    batch aborts before any write; BATCH_ABORTED names the failing edit's
    index (failed_edit_index) and its actual match count.

    dry_run=True returns per-edit match counts without writing.

    Large deletions trip a blast-radius guard (BLAST_RADIUS_EXCEEDED); pass
    confirm_delete_chars=<chars_deleted> from the error to proceed. Every
    successful write auto-snapshots a backup copy before writing; a
    confirmed blast-radius override does the same.

    Detects concurrent edits: if the file changed between read and write,
    returns CONCURRENT_MODIFICATION and writes nothing.

    Refuses trashed files (TRASHED_FILE), non-UTF-8 files (NOT_TEXT_FILE),
    unsupported MIME types (UNSUPPORTED_MIME), and files over 5MB
    (FILE_TOO_LARGE)."""
    if not edits:
        return {
            "error": "INVALID_INPUT",
            "retryable": False,
            "message": "edits array must not be empty.",
        }
    for i, edit in enumerate(edits):
        if "find" not in edit or "replace" not in edit:
            return {
                "error": "INVALID_INPUT",
                "retryable": False,
                "message": (
                    f"Edit at index {i} missing required field(s). "
                    f"Each edit must have 'find' and 'replace'."
                ),
            }

    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(
            fileId=file_id,
            fields="name,mimeType,size,modifiedTime,md5Checksum,trashed,trashedTime",
        )
        .execute()
    )
    if meta.get("trashed"):
        return _trashed_error(file_id, meta)

    try:
        blast_min_delta = int(os.environ.get("BLAST_RADIUS_MIN_DELTA", "200"))
        blast_max_ratio = float(os.environ.get("BLAST_RADIUS_MAX_RATIO", "2"))
        result = await text_ops.apply_edits_to_file(
            drive, file_id, meta, edits=edits,
            dry_run=dry_run,
            confirm_delete_chars=confirm_delete_chars,
            blast_min_delta=blast_min_delta,
            blast_max_ratio=blast_max_ratio,
            backup_folder_id=os.environ.get("BACKUP_FOLDER_ID"),
        )
        if "error" not in result:
            result["file_id"] = file_id
            result["file_name"] = meta.get("name", "")
            result["mime_type"] = meta.get("mimeType", "")
        return result
    except HttpError as exc:
        status = exc.resp.status if exc.resp else 0
        return {
            "error": "GOOGLE_API_ERROR",
            "retryable": status in TRANSIENT_CODES,
            "http_status": status,
            "message": (
                f"Google Drive API error (HTTP {status}) after retries: {exc}"
            ),
        }


@mcp.tool()
async def text_read_range(
    file_id: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    max_bytes: int = 100_000,
    cursor: Optional[str] = None,
) -> dict[str, Any]:
    """Read a bounded slice of a plain-text Drive file, to build a text_replace find string.

    start_line/end_line select an initial line range (0-indexed, inclusive);
    omit both to start at the top of the file. max_bytes caps the response
    size (default 100000) — follow the returned next_cursor in a follow-up
    call to continue past a truncated response; cursor takes precedence over
    start_line/end_line when both are given.

    Read-only. Same MIME allowlist (text/*, application/json,
    application/x-yaml) and 5MB size ceiling as text_replace."""
    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(fileId=file_id, fields="name,mimeType,size,trashed,trashedTime")
        .execute()
    )
    if meta.get("trashed"):
        return _trashed_error(file_id, meta)
    return await text_ops.read_range(
        drive, file_id, meta, start_line, end_line, max_bytes, cursor,
    )


@mcp.tool()
async def trash_file(file_id: str) -> dict[str, Any]:
    """Move a file to Drive trash. Reversible within 30 days via untrash_file."""
    try:
        return await drive_ops.trash_file(auth.get_drive_service(), file_id)
    except HttpError as exc:
        status = exc.resp.status if exc.resp else 0
        return {
            "error": "GOOGLE_API_ERROR",
            "retryable": status in TRANSIENT_CODES,
            "http_status": status,
            "message": f"Google Drive API error (HTTP {status}): {exc}",
        }


@mcp.tool()
async def untrash_file(file_id: str) -> dict[str, Any]:
    """Restore a trashed file from Drive trash."""
    try:
        return await drive_ops.untrash_file(auth.get_drive_service(), file_id)
    except HttpError as exc:
        status = exc.resp.status if exc.resp else 0
        return {
            "error": "GOOGLE_API_ERROR",
            "retryable": status in TRANSIENT_CODES,
            "http_status": status,
            "message": f"Google Drive API error (HTTP {status}): {exc}",
        }


@mcp.tool()
async def update_file_metadata(
    file_id: str,
    name: Optional[str] = None,
    add_parent_id: Optional[str] = None,
    remove_parent_id: Optional[str] = None,
) -> dict[str, Any]:
    """Rename a file and/or move it between folders, preserving its Drive file ID.

    Unlike copy+trash, this never mints a new file ID — every downstream
    reference (hardcoded file IDs in prompts, skills, scheduled tasks) keeps
    working after the rename/move.

    At least one of name, add_parent_id, remove_parent_id must be set, or
    this returns NO_CHANGES_REQUESTED and makes no API call.

    Reads the file's current name, parents and capabilities before mutating,
    so the response includes previous_name and previous_parents to verify
    the change or reverse it.

    Refuses trashed files with error: TRASHED_FILE (use untrash_file first;
    there is no sentinel value to trash a file here — use trash_file).
    Refuses rather than letting a raw Google 403 surface: CANNOT_RENAME when
    name is set and capabilities.canRename is false; CANNOT_MOVE when
    add_parent_id/remove_parent_id is set and
    capabilities.canMoveItemWithinDrive is false. Refuses a remove_parent_id
    that is not currently a parent of the file with error: NOT_A_PARENT
    (returns the actual parents list) rather than letting Drive no-op
    silently.
    """
    if name is None and add_parent_id is None and remove_parent_id is None:
        return {
            "error": "NO_CHANGES_REQUESTED",
            "retryable": False,
            "message": (
                "At least one of name, add_parent_id, remove_parent_id "
                "must be set."
            ),
        }

    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(
            fileId=file_id,
            fields="name,parents,capabilities,trashed,trashedTime",
        )
        .execute()
    )
    if meta.get("trashed"):
        return _trashed_error(file_id, meta)

    capabilities = meta.get("capabilities", {})
    if name is not None and not capabilities.get("canRename", True):
        return {
            "error": "CANNOT_RENAME",
            "retryable": False,
            "file_id": file_id,
            "message": "File capabilities disallow renaming (canRename is false).",
        }
    if (
        (add_parent_id is not None or remove_parent_id is not None)
        and not capabilities.get("canMoveItemWithinDrive", True)
    ):
        return {
            "error": "CANNOT_MOVE",
            "retryable": False,
            "file_id": file_id,
            "message": (
                "File capabilities disallow moving between folders "
                "(canMoveItemWithinDrive is false)."
            ),
        }

    previous_parents = meta.get("parents", [])
    if remove_parent_id is not None and remove_parent_id not in previous_parents:
        return {
            "error": "NOT_A_PARENT",
            "retryable": False,
            "file_id": file_id,
            "parents": previous_parents,
            "message": f"'{remove_parent_id}' is not a current parent of this file.",
        }

    try:
        result = await drive_ops.update_file_metadata(
            drive, file_id,
            name=name, add_parent_id=add_parent_id, remove_parent_id=remove_parent_id,
        )
        result["previous_name"] = meta.get("name", "")
        result["previous_parents"] = previous_parents
        return result
    except HttpError as exc:
        status = exc.resp.status if exc.resp else 0
        return {
            "error": "GOOGLE_API_ERROR",
            "retryable": status in TRANSIENT_CODES,
            "http_status": status,
            "message": f"Google Drive API error (HTTP {status}): {exc}",
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


@mcp.tool()
async def deliver_to_inbox(
    subject: str,
    body: str,
    content_type: str = "plain",
) -> dict[str, Any]:
    """Place a message into the authenticated user's own Gmail inbox.

    Uses Gmail API users.messages.insert — can only write to the
    authenticated user's mailbox, never transmits to third parties.
    From and To are hard-coded to josh@josh.is.

    Args:
        subject: Email subject line.
        body: Message body (plain text or HTML).
        content_type: 'plain' (default) or 'html'.
    """
    return await gmail_ops.deliver_to_inbox(
        gmail_service=auth.get_gmail_service(),
        subject=subject,
        body=body,
        content_type=content_type,
    )


@mcp.tool()
async def read_paragraph_at_path(
    file_id: str,
    path: str,
    include_children: bool = False,
) -> dict[str, Any]:
    """Read a paragraph by navigating a document's heading/list structure via path.

    Path segments are delimited by ' / ' (space-slash-space).
    Each segment matches by case-insensitive text prefix, or by positional
    index '#N' (1-based) among siblings.

    Example: "TASKS / Career / Careers that allow" or "TASKS / Career / #2"

    Args:
        file_id: Google Drive file ID of a native Google Doc.
        path: Path to the target paragraph.
        include_children: If True, include child paragraphs in the response.

    Only works on Google Docs (mimeType application/vnd.google-apps.document).
    """
    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(fileId=file_id, fields="name,mimeType")
        .execute()
    )
    if meta.get("mimeType") != GOOGLE_DOC_MIME:
        return {
            "error": "NOT_A_GOOGLE_DOC",
            "retryable": False,
            "message": (
                f"read_paragraph_at_path only works on Google Docs. "
                f"This file is {meta.get('mimeType')}."
            ),
        }
    docs = auth.get_docs_service()
    return await docs_ops.read_at_path(docs, file_id, path, include_children)


@mcp.tool()
async def read_thread(
    thread_id: str,
    strip_quoted_history: bool = False,
    message_limit: Optional[int] = None,
    cursor: Optional[str] = None,
    max_bytes: int = 100_000,
) -> dict[str, Any]:
    """Read a Gmail thread with bounded pagination and optional quote-stripping.

    Threads are append-only, so an offset cursor stays valid across calls; a
    concurrent new message sets ``thread_changed: true`` and pagination
    continues. Set ``strip_quoted_history`` to return each message's net-new
    body only. Follow ``next_cursor`` until it is null to read the full thread.

    Args:
        thread_id: Gmail thread ID.
        strip_quoted_history: Return only net-new body text per message.
        message_limit: Optional max messages per page.
        cursor: Opaque page token from a prior call.
        max_bytes: Response body size budget (default 100000).
    """
    return await gmail_ops.read_thread(
        auth.get_gmail_service(),
        thread_id,
        strip_quoted_history=strip_quoted_history,
        message_limit=message_limit,
        cursor=cursor,
        max_bytes=max_bytes,
    )


@mcp.tool()
async def read_document(
    file_id: str,
    fields: Optional[list[str]] = None,
    cursor: Optional[str] = None,
    max_bytes: int = 100_000,
) -> dict[str, Any]:
    """Read a Google Doc's body and/or comments with bounded pagination.

    ``fields`` selects the subset to return: ``["body"]``, ``["comments"]``, or
    both; omit for both. Body is paginated by structural element — follow
    ``next_cursor`` until null. If the doc changes mid-pagination the call
    returns ``STALE_CURSOR`` (restart from the beginning) rather than risk
    skipped/duplicated content.

    Args:
        file_id: Google Drive file ID of a native Google Doc.
        fields: Subset of ["body", "comments"]. Omit for both.
        cursor: Opaque page token from a prior call.
        max_bytes: Body size budget (default 100000).

    Only works on Google Docs (mimeType application/vnd.google-apps.document).
    """
    valid_fields = {"body", "comments"}
    if fields is not None and (not fields or set(fields) - valid_fields):
        return {
            "error": "INVALID_FIELDS",
            "retryable": False,
            "message": f"fields must be a non-empty subset of {sorted(valid_fields)}.",
        }

    drive = auth.get_drive_service()
    meta = await asyncio.to_thread(
        lambda: drive.files()
        .get(fileId=file_id, fields="mimeType,trashed,trashedTime")
        .execute()
    )
    if meta.get("mimeType") != GOOGLE_DOC_MIME:
        return {
            "error": "NOT_A_GOOGLE_DOC",
            "retryable": False,
            "message": (
                f"read_document only works on Google Docs. "
                f"This file is {meta.get('mimeType')}."
            ),
        }

    want_body = fields is None or "body" in fields
    want_comments = fields is None or "comments" in fields

    # A cursor paginates the body only. A comments-only request has nothing to
    # paginate, so a cursor there is a caller error — reject it explicitly
    # rather than silently discarding it.
    if not want_body and cursor is not None:
        return {
            "error": "INVALID_CURSOR",
            "retryable": False,
            "message": (
                "A cursor applies only to body reads; "
                "comments-only requests are not paginated."
            ),
        }

    result: dict[str, Any] = {"file_id": file_id}
    if meta.get("trashed"):
        result["trashed"] = True
        result["trashed_time"] = meta.get("trashedTime")

    if want_body:
        body = await docs_ops.read_document_body(
            auth.get_docs_service(), file_id, cursor=cursor, max_bytes=max_bytes
        )
        if body.get("error"):
            return body
        result.update(body)
    else:
        result["truncated"] = False
        result["next_cursor"] = None

    # Comments are unchanging across body pages, so return them on the first
    # page only (cursor is None). Later body pages omit them to avoid re-sending
    # the same list every page.
    if want_comments and cursor is None:
        comments = await drive_ops.list_comments(drive, file_id, include_resolved=True)
        result["comments"] = comments["comments"]
        result["comments_truncated"] = bool(comments.get("has_more"))

    return result


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
