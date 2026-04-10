"""Google Docs v1 operations — append, replace_text."""

import asyncio
from typing import Any


async def append_text_to_doc(
    docs_service, file_id: str, text: str
) -> dict[str, Any]:
    """Append text at end-of-body of a Google Doc. Preserves formatting."""
    doc = await asyncio.to_thread(
        lambda: docs_service.documents()
        .get(documentId=file_id, fields="body(content(endIndex))")
        .execute()
    )
    end_index = 1
    for element in doc.get("body", {}).get("content", []):
        end_index = max(end_index, element.get("endIndex", 1))
    insert_index = max(1, end_index - 1)
    requests = [
        {
            "insertText": {
                "location": {"index": insert_index},
                "text": text,
            }
        }
    ]
    await asyncio.to_thread(
        lambda: docs_service.documents()
        .batchUpdate(documentId=file_id, body={"requests": requests})
        .execute()
    )
    return {"bytes_appended": len(text.encode("utf-8"))}
