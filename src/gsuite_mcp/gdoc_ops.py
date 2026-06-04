"""Google Doc operations — template populate and suggest-edit via .docx export."""

import asyncio
import io
from typing import Any

from googleapiclient.http import MediaIoBaseUpload

from gsuite_mcp import docx_edits
from gsuite_mcp.retry import retry_transient


GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


async def template_populate(
    drive_service,
    docs_service,
    template_file_id: str,
    parent_folder_id: str,
    new_title: str,
    replacements: dict[str, str],
    post_styles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Copy a template file as a native Google Doc and replace placeholders.

    Uses Drive files.copy with mimeType conversion, then a single
    Docs batchUpdate with replaceAllText for each placeholder.
    """
    copy_body = {
        "name": new_title,
        "parents": [parent_folder_id],
        "mimeType": GOOGLE_DOC_MIME,
    }
    copied = await asyncio.to_thread(
        lambda: drive_service.files()
        .copy(
            fileId=template_file_id,
            body=copy_body,
            fields="id,name,webViewLink",
        )
        .execute()
    )
    new_file_id = copied["id"]

    if not replacements:
        result = {
            "file_id": new_file_id,
            "web_view_link": copied.get("webViewLink", ""),
            "replacements_made": {},
        }
        if post_styles:
            from gsuite_mcp import docs_ops

            style_result = await docs_ops.format_document(
                docs_service, new_file_id, post_styles
            )
            result["post_styles_result"] = style_result
        return result

    requests = [
        {
            "replaceAllText": {
                "containsText": {"text": placeholder, "matchCase": True},
                "replaceText": value,
            }
        }
        for placeholder, value in replacements.items()
    ]

    resp = await retry_transient(
        lambda: docs_service.documents()
        .batchUpdate(documentId=new_file_id, body={"requests": requests})
        .execute()
    )

    replacements_made = {}
    for (placeholder, _), reply in zip(replacements.items(), resp.get("replies", [])):
        count = reply.get("replaceAllText", {}).get("occurrencesChanged", 0)
        replacements_made[placeholder] = count

    result = {
        "file_id": new_file_id,
        "web_view_link": copied.get("webViewLink", ""),
        "replacements_made": replacements_made,
    }

    if post_styles:
        from gsuite_mcp import docs_ops

        style_result = await docs_ops.format_document(
            docs_service, new_file_id, post_styles
        )
        result["post_styles_result"] = style_result

    return result


async def suggest_edit(
    drive_service,
    file_id: str,
    find_text: str,
    replace_text: str,
    author: str = "Claude",
) -> dict[str, Any]:
    """Export a Google Doc as .docx, apply a tracked change, re-upload as new .docx.

    The original Google Doc is untouched. A new .docx file with tracked-change
    revision marks is created alongside it in the same parent folder.
    """
    meta = await asyncio.to_thread(
        lambda: drive_service.files()
        .get(fileId=file_id, fields="name,mimeType,parents")
        .execute()
    )

    if meta.get("mimeType") != GOOGLE_DOC_MIME:
        return {
            "error": "NOT_A_GOOGLE_DOC",
            "retryable": False,
            "message": (
                f"gdoc_suggest_edit only works on native Google Docs. "
                f"This file is {meta.get('mimeType')}. "
                f"For .docx files, use docx_suggest_edit directly."
            ),
        }

    exported = await asyncio.to_thread(
        lambda: drive_service.files()
        .export(fileId=file_id, mimeType=DOCX_MIME)
        .execute()
    )

    try:
        modified = docx_edits.insert_tracked_change(
            exported, find_text, replace_text, author
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

    original_name = meta.get("name", "Untitled")
    new_name = f"{original_name} (with suggestions).docx"
    parents = meta.get("parents", [])

    media = MediaIoBaseUpload(
        io.BytesIO(modified), mimetype=DOCX_MIME, resumable=True
    )
    body: dict[str, Any] = {"name": new_name}
    if parents:
        body["parents"] = parents

    result = await asyncio.to_thread(
        lambda: drive_service.files()
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
        "original_file_id": file_id,
        "modified_time": result.get("modifiedTime", ""),
        "note": (
            "A new .docx file with tracked-change suggestions has been created. "
            "Open it to review the suggestions. The original Google Doc is unchanged."
        ),
    }


async def batch_replace(
    drive_service,
    docs_service,
    file_id: str,
    edits: list[dict],
    dry_run: bool = False,
    confirm_delete_chars: int | None = None,
) -> dict[str, Any]:
    """Batch find/replace with Drive revision ID anchors.

    Wraps docs_ops.batch_replace, adding revision_id_before (always)
    and revision_id_after (only when committed) from Drive revisions.list.
    """
    from gsuite_mcp import docs_ops

    # Fetch latest Drive revision ID before mutation
    rev_resp = await asyncio.to_thread(
        lambda: drive_service.revisions()
        .list(fileId=file_id, fields="revisions(id)", pageSize=1000)
        .execute()
    )
    revisions = rev_resp.get("revisions", [])
    revision_id_before = revisions[-1]["id"] if revisions else None

    result = await docs_ops.batch_replace(
        docs_service, file_id, edits, dry_run=dry_run,
        confirm_delete_chars=confirm_delete_chars,
    )

    result["revision_id_before"] = revision_id_before

    if result.get("committed"):
        rev_resp_after = await asyncio.to_thread(
            lambda: drive_service.revisions()
            .list(fileId=file_id, fields="revisions(id)", pageSize=1000)
            .execute()
        )
        revisions_after = rev_resp_after.get("revisions", [])
        result["revision_id_after"] = revisions_after[-1]["id"] if revisions_after else None

    return result
