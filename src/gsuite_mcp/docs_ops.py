"""Google Docs v1 operations — append, replace_text."""

import asyncio
import re
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


async def replace_all_text(
    docs_service, file_id: str, find: str, replace: str, match_case: bool
) -> int:
    """Exact-match replace across a Google Doc. Returns occurrence count."""
    requests = [
        {
            "replaceAllText": {
                "containsText": {"text": find, "matchCase": match_case},
                "replaceText": replace,
            }
        }
    ]
    resp = await asyncio.to_thread(
        lambda: docs_service.documents()
        .batchUpdate(documentId=file_id, body={"requests": requests})
        .execute()
    )
    reply = resp.get("replies", [{}])[0]
    return reply.get("replaceAllText", {}).get("occurrencesChanged", 0)


async def replace_regex(
    docs_service, file_id: str, pattern: str, replacement: str, match_case: bool
) -> int:
    """Regex replace client-side via batched delete+insert requests."""
    flags = 0 if match_case else re.IGNORECASE
    regex = re.compile(pattern, flags)

    doc = await asyncio.to_thread(
        lambda: docs_service.documents()
        .get(documentId=file_id)
        .execute()
    )

    # Build (absolute_index, text) segments from all textRuns
    segments: list[tuple[int, str]] = []
    for block in doc.get("body", {}).get("content", []):
        para = block.get("paragraph")
        if not para:
            continue
        for elem in para.get("elements", []):
            tr = elem.get("textRun")
            if not tr:
                continue
            segments.append((elem["startIndex"], tr.get("content", "")))

    # Flatten into one big string with an index map
    flat_parts: list[str] = []
    index_map: list[int] = []  # index_map[i] = absolute doc index of char i
    for start_idx, text in segments:
        for offset, _ch in enumerate(text):
            flat_parts.append(_ch)
            index_map.append(start_idx + offset)
    flat = "".join(flat_parts)

    matches = list(regex.finditer(flat))
    if not matches:
        return 0

    # Build requests in REVERSE order so earlier-index edits don't shift later ones
    requests: list[dict] = []
    for m in reversed(matches):
        abs_start = index_map[m.start()]
        abs_end = index_map[m.end() - 1] + 1
        requests.append({
            "deleteContentRange": {
                "range": {"startIndex": abs_start, "endIndex": abs_end}
            }
        })
        requests.append({
            "insertText": {
                "location": {"index": abs_start},
                "text": m.expand(replacement),
            }
        })

    await asyncio.to_thread(
        lambda: docs_service.documents()
        .batchUpdate(documentId=file_id, body={"requests": requests})
        .execute()
    )
    return len(matches)
