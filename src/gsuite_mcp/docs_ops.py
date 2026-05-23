"""Google Docs v1 operations — append, replace_text, heading detection, format."""

import asyncio
import re
from typing import Any

from gsuite_mcp.retry import retry_transient

# ---------------------------------------------------------------------------
# Heading detection helpers
# ---------------------------------------------------------------------------

_HEADING_RANKS: dict[str, int] = {
    f"HEADING_{i}": i for i in range(1, 7)
}

_FALLBACK_RANK: int = 7

VALID_NAMED_STYLES: set[str] = {
    "NORMAL_TEXT",
    "TITLE",
    "SUBTITLE",
    *(f"HEADING_{i}" for i in range(1, 7)),
}

VALID_TEXT_STYLE_KEYS = {"bold", "italic", "underline", "strikethrough"}


def _validate_text_style(text_style: Any) -> str | None:
    """Validate a text_style dict. Returns error message or None if valid."""
    if not isinstance(text_style, dict):
        return "text_style must be a dict."
    if not text_style:
        return "text_style must contain at least one key from: bold, italic, underline, strikethrough."
    unknown = set(text_style.keys()) - VALID_TEXT_STYLE_KEYS
    if unknown:
        return f"Unknown text_style keys: {', '.join(sorted(unknown))}. Valid: bold, italic, underline, strikethrough."
    for key, val in text_style.items():
        if not isinstance(val, bool):
            return f"text_style['{key}'] must be a boolean, got {type(val).__name__}."
    return None


def _para_text(paragraph: dict) -> str:
    """Extract plain text from a paragraph's elements (concatenated textRuns)."""
    parts: list[str] = []
    for elem in paragraph.get("elements", []):
        tr = elem.get("textRun")
        if tr:
            parts.append(tr.get("content", ""))
    return "".join(parts)


def _flatten_doc_text(doc: dict) -> tuple[str, list[int]]:
    """Flatten all textRun content into a single string with an index map.

    Returns (flat_text, index_map) where index_map[i] is the absolute
    document index of flat_text[i].
    """
    flat_parts: list[str] = []
    index_map: list[int] = []
    for block in doc.get("body", {}).get("content", []):
        para = block.get("paragraph")
        if not para:
            continue
        for elem in para.get("elements", []):
            tr = elem.get("textRun")
            if not tr:
                continue
            start_idx = elem["startIndex"]
            content = tr.get("content", "")
            for offset, ch in enumerate(content):
                flat_parts.append(ch)
                index_map.append(start_idx + offset)
    return "".join(flat_parts), index_map


def _count_occurrences(
    flat_text: str, find: str, *, match_case: bool, regex: bool
) -> int:
    """Count occurrences of *find* in *flat_text*."""
    if regex:
        flags = 0 if match_case else re.IGNORECASE
        return len(re.findall(find, flat_text, flags))
    if not match_case:
        return flat_text.casefold().count(find.casefold())
    return flat_text.count(find)


def _find_heading(
    doc: dict,
    section_heading: str,
    matches_out: list | None = None,
) -> dict[str, Any] | None:
    """Locate a section heading inside a Google Docs document structure.

    Pass 1 — formal headings (HEADING_1 through HEADING_6).
    Pass 2 — text fallback (any paragraph whose stripped text matches).

    Returns a dict with ``text``, ``start_index``, ``end_index``,
    ``heading_level``, ``level_rank``, and ``paragraph_index`` when exactly
    one match is found.  Returns ``None`` on zero or multiple matches;
    populates *matches_out* (if provided) when ambiguous.

    When Pass 1 produces multiple formal matches, Pass 2 is not attempted
    and *matches_out* contains only the formal matches.
    """
    content = doc.get("body", {}).get("content", [])
    needle = section_heading.strip().lower()

    def _build_match(block: dict, para_idx: int, named_style: str) -> dict:
        raw_text = _para_text(block["paragraph"])
        return {
            "text": raw_text.strip(),
            "start_index": block["startIndex"],
            "end_index": block["endIndex"],
            "heading_level": named_style,
            "level_rank": _HEADING_RANKS.get(named_style, _FALLBACK_RANK),
            "paragraph_index": para_idx,
        }

    # Pass 1 — formal headings
    formal: list[dict] = []
    for idx, block in enumerate(content):
        para = block.get("paragraph")
        if not para:
            continue
        style = para.get("paragraphStyle", {}).get("namedStyleType", "")
        if style not in _HEADING_RANKS:
            continue
        text = _para_text(para).strip().lower()
        if text == needle:
            formal.append(_build_match(block, idx, style))

    if len(formal) == 1:
        return formal[0]
    if formal:
        if matches_out is not None:
            matches_out.extend(formal)
        return None

    # Pass 2 — text fallback (no formal heading matched)
    fallback: list[dict] = []
    for idx, block in enumerate(content):
        para = block.get("paragraph")
        if not para:
            continue
        style = para.get("paragraphStyle", {}).get("namedStyleType", "")
        text = _para_text(para).strip().lower()
        if text == needle:
            fallback.append(_build_match(block, idx, style))

    if len(fallback) == 1:
        return fallback[0]
    if fallback:
        if matches_out is not None:
            matches_out.extend(fallback)
    return None


def _find_section_end(doc: dict, heading: dict[str, Any]) -> int:
    """Find the end index of a section starting at *heading*.

    Scans forward from the heading's position through the document's content
    blocks.  The section ends just before the next paragraph whose heading
    level is **<=** the matched heading's ``level_rank`` (i.e. same or higher
    importance).

    For fallback headings (``level_rank == _FALLBACK_RANK``), *any* formal
    heading (HEADING_1 through HEADING_6) terminates the section.

    If no terminating heading is found, returns the ``endIndex`` of the last
    content block (the section extends to the end of the document).
    """
    content = doc.get("body", {}).get("content", [])
    start_para = heading["paragraph_index"]
    level_rank = heading["level_rank"]

    for block in content[start_para + 1 :]:
        para = block.get("paragraph")
        if not para:
            continue
        style = para.get("paragraphStyle", {}).get("namedStyleType", "")
        if style not in _HEADING_RANKS:
            continue
        next_rank = _HEADING_RANKS[style]
        # For fallback headings any formal heading ends the section;
        # for formal headings only same-or-higher level (lower-or-equal rank).
        if level_rank == _FALLBACK_RANK or next_rank <= level_rank:
            return block["startIndex"]

    # No terminating heading found — section extends to end of document.
    if content:
        return content[-1]["endIndex"]
    return 0


async def replace_section(
    docs_service,
    file_id: str,
    section_heading: str,
    new_content: str,
    include_heading: bool = False,
) -> dict[str, Any]:
    """Replace the body (or body + heading) of a document section.

    Locates *section_heading* via ``_find_heading``, determines the section
    boundary with ``_find_section_end``, then issues a single ``batchUpdate``
    that deletes the old content and inserts *new_content* styled as
    ``NORMAL_TEXT``.  When *include_heading* is ``True`` the heading itself
    is also replaced and its original ``namedStyleType`` is reapplied to the
    first paragraph of the inserted text.
    """
    doc = await asyncio.to_thread(
        lambda: docs_service.documents()
        .get(documentId=file_id)
        .execute()
    )

    matches: list[dict] = []
    heading = _find_heading(doc, section_heading, matches_out=matches)

    if heading is None and not matches:
        return {
            "error": "HEADING_NOT_FOUND",
            "retryable": False,
            "message": f"Heading '{section_heading}' not found in document.",
        }

    if heading is None and matches:
        return {
            "error": "AMBIGUOUS_HEADING",
            "retryable": False,
            "message": (
                f"Multiple headings match '{section_heading}'. "
                "Provide a more specific heading."
            ),
            "matches": [
                {
                    "text": m["text"],
                    "start_index": m["start_index"],
                    "heading_level": m["heading_level"],
                }
                for m in matches
            ],
        }

    content = doc.get("body", {}).get("content", [])
    section_end = _find_section_end(doc, heading)

    delete_start = heading["start_index"] if include_heading else heading["end_index"]
    delete_end = section_end

    # Detect empty section body (heading immediately followed by next heading)
    empty_section = delete_start >= delete_end and not include_heading

    # Ensure trailing newline
    if not new_content.endswith("\n"):
        new_content += "\n"

    characters_inserted = len(new_content)

    if empty_section:
        # No body to delete — insert after the heading
        insert_index = heading["end_index"]
        characters_deleted = 0

        requests: list[dict] = [
            {
                "insertText": {
                    "location": {"index": insert_index},
                    "text": new_content,
                }
            },
            {
                "updateParagraphStyle": {
                    "range": {
                        "startIndex": insert_index,
                        "endIndex": insert_index + characters_inserted,
                    },
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "fields": "namedStyleType",
                }
            },
        ]
    else:
        characters_deleted = delete_end - delete_start

        # Clamp delete range to avoid the structural trailing newline
        delete_end_clamped = _clamp_delete_end(delete_end, content)

        requests = [
            {
                "deleteContentRange": {
                    "range": {
                        "startIndex": delete_start,
                        "endIndex": delete_end_clamped,
                    }
                }
            },
            {
                "insertText": {
                    "location": {"index": delete_start},
                    "text": new_content,
                }
            },
            {
                "updateParagraphStyle": {
                    "range": {
                        "startIndex": delete_start,
                        "endIndex": delete_start + characters_inserted,
                    },
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "fields": "namedStyleType",
                }
            },
        ]

        # Restore heading style on first inserted paragraph when include_heading
        if include_heading and heading["heading_level"] in _HEADING_RANKS:
            first_newline = new_content.find("\n")
            first_para_end = delete_start + first_newline + 1
            requests.append({
                "updateParagraphStyle": {
                    "range": {
                        "startIndex": delete_start,
                        "endIndex": first_para_end,
                    },
                    "paragraphStyle": {
                        "namedStyleType": heading["heading_level"],
                    },
                    "fields": "namedStyleType",
                }
            })

    await retry_transient(
        lambda: docs_service.documents()
        .batchUpdate(documentId=file_id, body={"requests": requests})
        .execute()
    )

    return {
        "file_id": file_id,
        "section_heading": heading["text"],
        "heading_level": heading["heading_level"],
        "characters_deleted": characters_deleted,
        "characters_inserted": characters_inserted,
        "include_heading": include_heading,
    }


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
    await retry_transient(
        lambda: docs_service.documents()
        .batchUpdate(documentId=file_id, body={"requests": requests})
        .execute()
    )
    return {"bytes_appended": len(text.encode("utf-8"))}


async def replace_all_text(
    docs_service, file_id: str, find: str, replace: str, match_case: bool,
    expected_count: int | None = None,
) -> int | dict[str, Any]:
    """Exact-match replace across a Google Doc. Returns occurrence count.

    When expected_count is set, fetches the document first and counts
    occurrences client-side. Returns a COUNT_MISMATCH error dict if
    the count doesn't match -- no mutation occurs.
    """
    if expected_count is not None:
        doc = await asyncio.to_thread(
            lambda: docs_service.documents()
            .get(documentId=file_id)
            .execute()
        )
        flat, _ = _flatten_doc_text(doc)
        actual = _count_occurrences(flat, find, match_case=match_case, regex=False)
        if actual != expected_count:
            return {
                "error": "COUNT_MISMATCH",
                "retryable": False,
                "expected_count": expected_count,
                "actual_count": actual,
                "message": f"Expected {expected_count} occurrence(s) but found {actual}. No changes made.",
            }

    requests = [
        {
            "replaceAllText": {
                "containsText": {"text": find, "matchCase": match_case},
                "replaceText": replace,
            }
        }
    ]
    resp = await retry_transient(
        lambda: docs_service.documents()
        .batchUpdate(documentId=file_id, body={"requests": requests})
        .execute()
    )
    reply = resp.get("replies", [{}])[0]
    return reply.get("replaceAllText", {}).get("occurrencesChanged", 0)


async def replace_regex(
    docs_service, file_id: str, pattern: str, replacement: str, match_case: bool,
    expected_count: int | None = None,
) -> int | dict[str, Any]:
    """Regex replace client-side via batched delete+insert requests."""
    flags = 0 if match_case else re.IGNORECASE
    regex = re.compile(pattern, flags)

    doc = await asyncio.to_thread(
        lambda: docs_service.documents()
        .get(documentId=file_id)
        .execute()
    )

    flat, index_map = _flatten_doc_text(doc)

    matches = list(regex.finditer(flat))
    if expected_count is not None and len(matches) != expected_count:
        return {
            "error": "COUNT_MISMATCH",
            "retryable": False,
            "expected_count": expected_count,
            "actual_count": len(matches),
            "message": f"Expected {expected_count} occurrence(s) but found {len(matches)}. No changes made.",
        }
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

    await retry_transient(
        lambda: docs_service.documents()
        .batchUpdate(documentId=file_id, body={"requests": requests})
        .execute()
    )
    return len(matches)


# ---------------------------------------------------------------------------
# Context-anchored replacement
# ---------------------------------------------------------------------------

CONTEXT_WINDOW = 200


def _find_all_substring(
    text: str, needle: str, match_case: bool
) -> list[tuple[int, int]]:
    """Find all (start, end) positions of needle in text."""
    if not match_case:
        search_text = text.casefold()
        search_needle = needle.casefold()
    else:
        search_text = text
        search_needle = needle
    results: list[tuple[int, int]] = []
    start = 0
    while True:
        idx = search_text.find(search_needle, start)
        if idx == -1:
            break
        results.append((idx, idx + len(needle)))
        start = idx + 1
    return results


def _normalize_matches(
    matches: list, regex: bool
) -> list[tuple[int, int]]:
    """Normalize regex Match objects or (start, end) tuples to (start, end)."""
    if regex:
        return [(m.start(), m.end()) for m in matches]
    return matches


async def replace_in_context(
    docs_service,
    file_id: str,
    find: str,
    replace: str,
    match_case: bool,
    preceded_by: str | None = None,
    followed_by: str | None = None,
    regex: bool = False,
    expected_count: int | None = None,
) -> int | dict[str, Any]:
    """Replace text with context-anchor filtering.

    Finds all occurrences of *find*, then keeps only those where
    *preceded_by* appears within 200 chars before the match and/or
    *followed_by* appears within 200 chars after. Executes filtered
    replacements as a single batchUpdate.
    """
    doc = await asyncio.to_thread(
        lambda: docs_service.documents()
        .get(documentId=file_id)
        .execute()
    )
    flat, index_map = _flatten_doc_text(doc)

    if regex:
        flags = 0 if match_case else re.IGNORECASE
        pattern = re.compile(find, flags)
        raw_matches = list(pattern.finditer(flat))
    else:
        raw_matches = _find_all_substring(flat, find, match_case)

    # Filter by context anchors
    filtered: list[tuple[int, int]] = []
    for m_start, m_end in _normalize_matches(raw_matches, regex):
        if preceded_by is not None:
            window_start = max(0, m_start - CONTEXT_WINDOW)
            before_text = flat[window_start:m_start]
            if match_case:
                if preceded_by not in before_text:
                    continue
            else:
                if preceded_by.casefold() not in before_text.casefold():
                    continue
        if followed_by is not None:
            window_end = min(len(flat), m_end + CONTEXT_WINDOW)
            after_text = flat[m_end:window_end]
            if match_case:
                if followed_by not in after_text:
                    continue
            else:
                if followed_by.casefold() not in after_text.casefold():
                    continue
        filtered.append((m_start, m_end))

    if expected_count is not None and len(filtered) != expected_count:
        return {
            "error": "COUNT_MISMATCH",
            "retryable": False,
            "expected_count": expected_count,
            "actual_count": len(filtered),
            "message": (
                f"Expected {expected_count} occurrence(s) but found "
                f"{len(filtered)} after context filtering. No changes made."
            ),
        }

    if not filtered:
        return 0

    # Build requests in REVERSE order so earlier-index edits don't shift later ones
    requests: list[dict] = []
    for m_start, m_end in reversed(filtered):
        abs_start = index_map[m_start]
        abs_end = index_map[m_end - 1] + 1
        if regex:
            flags = 0 if match_case else re.IGNORECASE
            replacement_text = re.compile(find, flags).search(
                flat[m_start:m_end]
            ).expand(replace)
        else:
            replacement_text = replace
        requests.append({
            "deleteContentRange": {
                "range": {"startIndex": abs_start, "endIndex": abs_end}
            }
        })
        requests.append({
            "insertText": {
                "location": {"index": abs_start},
                "text": replacement_text,
            }
        })

    await retry_transient(
        lambda: docs_service.documents()
        .batchUpdate(documentId=file_id, body={"requests": requests})
        .execute()
    )
    return len(filtered)


# ---------------------------------------------------------------------------
# Document tree builder for path-based navigation
# ---------------------------------------------------------------------------


def _build_doc_tree(content: list[dict]) -> list[dict]:
    """Build a tree from document content using headings and bullet nesting.

    Returns a list of top-level nodes. Each node has:
    - text: paragraph text (stripped)
    - start_index, end_index: document indices
    - paragraph_index: index in content list
    - nesting_level: 0 for headings, bullet nestingLevel for list items
    - heading_level: named style if a heading, else None
    - children: list of child nodes
    """
    def _make_node(block: dict, block_idx: int) -> dict:
        para = block["paragraph"]
        text = _para_text(para).strip()
        style = para.get("paragraphStyle", {}).get("namedStyleType", "")
        bullet = para.get("bullet")
        nesting = bullet["nestingLevel"] if bullet else 0
        return {
            "text": text,
            "start_index": block["startIndex"],
            "end_index": block["endIndex"],
            "paragraph_index": block_idx,
            "nesting_level": nesting,
            "heading_level": style if style in _HEADING_RANKS else None,
            "children": [],
        }

    # Collect all paragraph nodes
    all_nodes: list[dict] = []
    for idx, block in enumerate(content):
        if not block.get("paragraph"):
            continue
        all_nodes.append(_make_node(block, idx))

    if not all_nodes:
        return []

    # Build tree: headings are top-level, bullets nest by nestingLevel
    root: list[dict] = []
    stack: list[tuple[dict, int]] = []  # (node, depth)

    for node in all_nodes:
        if node["heading_level"] is not None:
            # Heading: always top-level
            root.append(node)
            stack = [(node, -1)]
        else:
            depth = node["nesting_level"]
            # Pop stack until we find a parent at a lower depth
            while stack and stack[-1][1] >= depth:
                stack.pop()
            if stack:
                stack[-1][0]["children"].append(node)
            else:
                root.append(node)
            stack.append((node, depth))

    return root


def _resolve_path(
    tree: list[dict], segments: list[str]
) -> dict | None:
    """Walk the tree by path segments, returning the matched node or None.

    Each segment matches by:
    1. Case-insensitive text prefix (first match wins)
    2. Positional #N (1-based) if segment starts with '#'
    """
    current_children = tree
    node = None

    for segment in segments:
        segment = segment.strip()
        found = None

        if segment.startswith("#") and segment[1:].isdigit():
            # Positional index (1-based)
            pos = int(segment[1:])
            if 1 <= pos <= len(current_children):
                found = current_children[pos - 1]
        else:
            # Text prefix match (case-insensitive)
            needle = segment.casefold()
            for child in current_children:
                if child["text"].casefold().startswith(needle):
                    found = child
                    break

        if found is None:
            return None
        node = found
        current_children = found.get("children", [])

    return node


async def read_at_path(
    docs_service,
    file_id: str,
    path: str,
    include_children: bool = False,
) -> dict[str, Any]:
    """Read a paragraph by navigating a path through the document tree."""
    doc = await asyncio.to_thread(
        lambda: docs_service.documents()
        .get(documentId=file_id)
        .execute()
    )
    content = doc.get("body", {}).get("content", [])
    tree = _build_doc_tree(content)
    segments = [s.strip() for s in path.split(" / ")]
    node = _resolve_path(tree, segments)

    if node is None:
        return {
            "error": "PATH_NOT_FOUND",
            "retryable": False,
            "message": f"No paragraph found at path: {path}",
        }

    result: dict[str, Any] = {
        "file_id": file_id,
        "path": path,
        "text": node["text"],
        "paragraph_index": node["paragraph_index"],
        "nesting_level": node["nesting_level"],
        "start_index": node["start_index"],
        "end_index": node["end_index"],
    }
    if include_children:
        result["children"] = [
            {
                "text": c["text"],
                "nesting_level": c["nesting_level"],
                "paragraph_index": c["paragraph_index"],
                "start_index": c["start_index"],
                "end_index": c["end_index"],
            }
            for c in node.get("children", [])
        ]
    return result


# ---------------------------------------------------------------------------
# Batched document formatting
# ---------------------------------------------------------------------------


def _find_paragraphs_matching(
    content: list[dict],
    find_text: str,
    *,
    substring: bool = False,
    match_mode: str = "exact",
) -> list[tuple[int, dict]]:
    """Return all (block_index, block) pairs whose paragraph text matches.

    match_mode controls matching behavior:
    - "exact": strip + casefold equality (default)
    - "substring": needle-in-text after strip + casefold
    - "regex": ``re.search(find_text, text)`` — case-sensitive, no casefold

    match_mode takes precedence over the legacy ``substring`` flag.
    When match_mode is "exact" and substring=True, falls back to substring mode.
    """
    matches: list[tuple[int, dict]] = []
    effective_mode = match_mode if match_mode != "exact" else ("substring" if substring else "exact")

    if effective_mode == "regex":
        pattern = re.compile(find_text)
        for idx, block in enumerate(content):
            para = block.get("paragraph")
            if not para:
                continue
            text = _para_text(para).strip()
            if pattern.search(text):
                matches.append((idx, block))
    else:
        needle = find_text.strip().casefold()
        for idx, block in enumerate(content):
            para = block.get("paragraph")
            if not para:
                continue
            text = _para_text(para).strip().casefold()
            if effective_mode == "substring":
                if needle in text:
                    matches.append((idx, block))
            else:
                if text == needle:
                    matches.append((idx, block))
    return matches


def _doc_body_end_index(content: list[dict]) -> int:
    """Return the body's endIndex (last content block's endIndex), or 0."""
    if content:
        return content[-1]["endIndex"]
    return 0


def _clamp_delete_end(end_index: int, content: list[dict]) -> int:
    """Clamp endIndex to avoid the structural trailing newline.

    If end_index equals (or exceeds) the document body's endIndex, subtract 1.
    """
    body_end = _doc_body_end_index(content)
    if body_end > 0 and end_index >= body_end:
        return body_end - 1
    return end_index


async def format_document(
    docs_service,
    file_id: str,
    operations: list[dict[str, Any]],
    *,
    preview: bool = False,
) -> dict[str, Any]:
    """Apply a batch of formatting operations to a Google Doc.

    Each operation is a dict with an ``action`` key and action-specific fields:

    - ``{"action": "set_style", "find_text": "...", "style": "HEADING_1"}``
      Change the paragraph style.  Matching is exact (strip + casefold).
      Add ``"substring": true`` for legacy substring matching.
      If multiple paragraphs match, fails with ``multi_match_error`` unless
      ``"match_all": true`` is set on the operation.

    - ``{"action": "delete", "find_text": "..."}``
      Delete a paragraph.  Same matching rules as set_style.

    - ``{"action": "delete_by_index", "paragraph_index": N}``
      Delete the paragraph at content index *N* (from a prior document read).

    - ``{"action": "delete_empty_after", "find_text": "..."}``
      Delete consecutive empty/whitespace-only paragraphs immediately after
      the first matching paragraph.

    Top-level options:

    - ``preview=True``: Return the list of paragraphs each operation would
      affect (paragraph index + first 80 chars + action) without executing.

    Operations that cannot find their target are reported as ``not_found``
    but do not block other operations.
    """
    # -- Validate ----------------------------------------------------------
    if not operations:
        return {
            "error": "EMPTY_OPERATIONS",
            "retryable": False,
            "message": "operations list must contain at least one operation.",
        }

    valid_actions = {"set_style", "set_text_style", "delete", "delete_empty_after", "delete_by_index", "insert_paragraph", "insert_paragraph_after_match"}
    for i, op in enumerate(operations):
        action = op.get("action")
        if action not in valid_actions:
            return {
                "error": "INVALID_ACTION",
                "retryable": False,
                "message": (
                    f"Operation {i}: unknown action '{action}'. "
                    f"Valid actions: {', '.join(sorted(valid_actions))}."
                ),
            }
        if action == "delete_by_index":
            pi = op.get("paragraph_index")
            if not isinstance(pi, int):
                return {
                    "error": "MISSING_PARAGRAPH_INDEX",
                    "retryable": False,
                    "message": f"Operation {i}: 'paragraph_index' is required and must be an integer.",
                }
        elif action == "insert_paragraph":
            pi = op.get("after_paragraph_index")
            if not isinstance(pi, int):
                return {
                    "error": "MISSING_PARAGRAPH_INDEX",
                    "retryable": False,
                    "message": f"Operation {i}: 'after_paragraph_index' is required and must be an integer.",
                }
            text = op.get("text")
            if not isinstance(text, str) or not text.strip():
                return {
                    "error": "MISSING_TEXT",
                    "retryable": False,
                    "message": f"Operation {i}: 'text' is required and must be non-blank.",
                }
            ts = op.get("text_style")
            if ts is not None:
                err = _validate_text_style(ts)
                if err:
                    return {
                        "error": "INVALID_TEXT_STYLE",
                        "retryable": False,
                        "message": f"Operation {i}: {err}",
                    }
            nl = op.get("nesting_level")
            if nl is not None and (not isinstance(nl, int) or nl < 0):
                return {
                    "error": "INVALID_NESTING_LEVEL",
                    "retryable": False,
                    "message": f"Operation {i}: 'nesting_level' must be a non-negative integer.",
                }
        else:
            find_text = op.get("find_text", "")
            if not isinstance(find_text, str) or not find_text.strip():
                return {
                    "error": "MISSING_FIND_TEXT",
                    "retryable": False,
                    "message": f"Operation {i}: 'find_text' is required and must be non-blank.",
                }
        if action not in ("delete_by_index", "insert_paragraph"):
            mm = op.get("match_mode", "exact")
            if mm not in ("exact", "substring", "regex"):
                return {
                    "error": "INVALID_MATCH_MODE",
                    "retryable": False,
                    "message": (
                        f"Operation {i}: invalid match_mode '{mm}'. "
                        f"Valid: exact, substring, regex."
                    ),
                }
            if mm == "regex":
                try:
                    re.compile(op.get("find_text", ""))
                except re.error as exc:
                    return {
                        "error": "INVALID_REGEX",
                        "retryable": False,
                        "message": f"Operation {i}: invalid regex '{op.get('find_text', '')}': {exc}",
                    }
        if action == "set_style":
            style = op.get("style")
            if style not in VALID_NAMED_STYLES:
                return {
                    "error": "INVALID_STYLE",
                    "retryable": False,
                    "message": (
                        f"Operation {i}: invalid style '{style}'. "
                        f"Valid styles: {', '.join(sorted(VALID_NAMED_STYLES))}."
                    ),
                }
        if action == "set_text_style":
            ts = op.get("style")
            err = _validate_text_style(ts)
            if err:
                return {
                    "error": "INVALID_TEXT_STYLE",
                    "retryable": False,
                    "message": f"Operation {i}: {err}",
                }

    # -- Fetch document ----------------------------------------------------
    doc = await asyncio.to_thread(
        lambda: docs_service.documents()
        .get(documentId=file_id)
        .execute()
    )
    content = doc.get("body", {}).get("content", [])

    # -- Resolve operations to API requests --------------------------------
    # Each entry: (startIndex, api_request_dict)
    pending: list[tuple[int, dict]] = []
    results: list[dict[str, Any]] = []

    for op in operations:
        action = op["action"]

        # --- delete_by_index: no string matching --------------------------
        if action == "delete_by_index":
            para_idx = op["paragraph_index"]
            if para_idx < 0 or para_idx >= len(content):
                results.append({
                    "action": "delete_by_index",
                    "paragraph_index": para_idx,
                    "status": "index_out_of_range",
                })
                continue
            block = content[para_idx]
            if not block.get("paragraph"):
                results.append({
                    "action": "delete_by_index",
                    "paragraph_index": para_idx,
                    "status": "not_a_paragraph",
                })
                continue
            end_idx = _clamp_delete_end(block["endIndex"], content)
            chars = end_idx - block["startIndex"]
            text_snippet = _para_text(block["paragraph"]).strip()[:80]
            if preview:
                results.append({
                    "action": "delete_by_index",
                    "paragraph_index": para_idx,
                    "text": text_snippet,
                    "status": "would_apply",
                })
            else:
                pending.append((block["startIndex"], {
                    "deleteContentRange": {
                        "range": {
                            "startIndex": block["startIndex"],
                            "endIndex": end_idx,
                        }
                    }
                }))
                results.append({
                    "action": "delete_by_index",
                    "paragraph_index": para_idx,
                    "status": "applied",
                    "characters_deleted": chars,
                })
            continue

        # --- insert_paragraph: index-based insert --------------------------
        if action == "insert_paragraph":
            para_idx = op["after_paragraph_index"]
            if para_idx < 0 or para_idx >= len(content):
                results.append({
                    "action": "insert_paragraph",
                    "after_paragraph_index": para_idx,
                    "status": "index_out_of_range",
                })
                continue
            block = content[para_idx]
            if not block.get("paragraph"):
                results.append({
                    "action": "insert_paragraph",
                    "after_paragraph_index": para_idx,
                    "status": "not_a_paragraph",
                })
                continue

            insert_text = op["text"]
            if not insert_text.endswith("\n"):
                insert_text += "\n"
            insert_index = block["endIndex"]
            text_style = op.get("text_style")
            nesting_override = op.get("nesting_level")

            if preview:
                results.append({
                    "action": "insert_paragraph",
                    "after_paragraph_index": para_idx,
                    "text": insert_text.strip()[:80],
                    "status": "would_apply",
                })
            else:
                # insertText
                pending.append((insert_index, {
                    "insertText": {
                        "location": {"index": insert_index},
                        "text": insert_text,
                    }
                }))
                # updateTextStyle if text_style provided
                if text_style:
                    fields_mask = ",".join(sorted(text_style.keys()))
                    pending.append((insert_index, {
                        "updateTextStyle": {
                            "range": {
                                "startIndex": insert_index,
                                "endIndex": insert_index + len(insert_text),
                            },
                            "textStyle": text_style,
                            "fields": fields_mask,
                        }
                    }))
                # nesting_level override via paragraph style
                if nesting_override is not None:
                    indent = nesting_override * 36  # 36pt per nesting level (Google Docs default)
                    pending.append((insert_index, {
                        "updateParagraphStyle": {
                            "range": {
                                "startIndex": insert_index,
                                "endIndex": insert_index + len(insert_text),
                            },
                            "paragraphStyle": {
                                "indentStart": {"magnitude": indent, "unit": "PT"},
                                "indentFirstLine": {"magnitude": indent, "unit": "PT"},
                            },
                            "fields": "indentStart,indentFirstLine",
                        }
                    }))
                results.append({
                    "action": "insert_paragraph",
                    "after_paragraph_index": para_idx,
                    "status": "applied",
                    "characters_inserted": len(insert_text),
                })
            continue

        # --- Text-matching actions ----------------------------------------
        find_text = op["find_text"]
        mm = op.get("match_mode", "exact")
        # match_mode takes precedence; only fall back to substring flag
        # when match_mode is at its default ("exact") and not explicitly set.
        if "match_mode" in op:
            use_substring = False
        else:
            use_substring = bool(op.get("substring", False))
        matches = _find_paragraphs_matching(
            content, find_text, substring=use_substring, match_mode=mm,
        )

        if not matches:
            results.append({
                "action": action,
                "find_text": find_text,
                "status": "not_found",
            })
            continue

        # Multi-match protection for delete and set_style
        if action in ("delete", "set_style", "set_text_style") and len(matches) > 1:
            if not op.get("match_all", False):
                results.append({
                    "action": action,
                    "find_text": find_text,
                    "status": "multi_match_error",
                    "matches": [
                        {
                            "paragraph_index": idx,
                            "text": _para_text(blk["paragraph"]).strip()[:80],
                        }
                        for idx, blk in matches
                    ],
                })
                continue

        if action == "set_style":
            style = op["style"]
            total_applied = 0
            for block_idx, block in matches:
                text_snippet = _para_text(block["paragraph"]).strip()[:80]
                if preview:
                    results.append({
                        "action": "set_style",
                        "find_text": find_text,
                        "style": style,
                        "paragraph_index": block_idx,
                        "text": text_snippet,
                        "status": "would_apply",
                    })
                else:
                    pending.append((block["startIndex"], {
                        "updateParagraphStyle": {
                            "range": {
                                "startIndex": block["startIndex"],
                                "endIndex": block["endIndex"],
                            },
                            "paragraphStyle": {"namedStyleType": style},
                            "fields": "namedStyleType",
                        }
                    }))
                    total_applied += 1
            if not preview:
                # Single result entry for the operation
                results.append({
                    "action": "set_style",
                    "find_text": find_text,
                    "style": style,
                    "status": "applied",
                    "start_index": matches[0][1]["startIndex"],
                })

        elif action == "set_text_style":
            style = op["style"]
            fields_mask = ",".join(sorted(style.keys()))
            for block_idx, block in matches:
                text_snippet = _para_text(block["paragraph"]).strip()[:80]
                if preview:
                    results.append({
                        "action": "set_text_style",
                        "find_text": find_text,
                        "style": style,
                        "paragraph_index": block_idx,
                        "text": text_snippet,
                        "status": "would_apply",
                    })
                else:
                    pending.append((block["startIndex"], {
                        "updateTextStyle": {
                            "range": {
                                "startIndex": block["startIndex"],
                                "endIndex": block["endIndex"],
                            },
                            "textStyle": style,
                            "fields": fields_mask,
                        }
                    }))
            if not preview:
                results.append({
                    "action": "set_text_style",
                    "find_text": find_text,
                    "style": style,
                    "status": "applied",
                    "start_index": matches[0][1]["startIndex"],
                })

        elif action == "delete":
            total_chars = 0
            for block_idx, block in matches:
                end_idx = _clamp_delete_end(block["endIndex"], content)
                chars = end_idx - block["startIndex"]
                text_snippet = _para_text(block["paragraph"]).strip()[:80]
                if preview:
                    results.append({
                        "action": "delete",
                        "find_text": find_text,
                        "paragraph_index": block_idx,
                        "text": text_snippet,
                        "status": "would_apply",
                    })
                else:
                    pending.append((block["startIndex"], {
                        "deleteContentRange": {
                            "range": {
                                "startIndex": block["startIndex"],
                                "endIndex": end_idx,
                            }
                        }
                    }))
                    total_chars += chars
            if not preview:
                results.append({
                    "action": "delete",
                    "find_text": find_text,
                    "status": "applied",
                    "characters_deleted": total_chars,
                })

        elif action == "delete_empty_after":
            # Use first match only (non-destructive to matched paragraph)
            block_idx, block = matches[0]
            empty_count = 0
            deleted_chars = 0
            scan = block_idx + 1
            while scan < len(content):
                next_block = content[scan]
                next_para = next_block.get("paragraph")
                if not next_para:
                    break
                next_text = _para_text(next_para).strip()
                if next_text:
                    break
                end_idx = _clamp_delete_end(next_block["endIndex"], content)
                chars = end_idx - next_block["startIndex"]
                if not preview:
                    pending.append((next_block["startIndex"], {
                        "deleteContentRange": {
                            "range": {
                                "startIndex": next_block["startIndex"],
                                "endIndex": end_idx,
                            }
                        }
                    }))
                empty_count += 1
                deleted_chars += chars
                scan += 1

            results.append({
                "action": "delete_empty_after",
                "find_text": find_text,
                "status": "would_apply" if preview else "applied",
                "empty_paragraphs_deleted": empty_count,
                "characters_deleted": deleted_chars,
            })

    # -- Execute -----------------------------------------------------------
    if preview:
        return {
            "file_id": file_id,
            "preview": True,
            "results": results,
        }

    applied = len([r for r in results if r["status"] == "applied"])

    if pending:
        # Sort by startIndex descending so deletions don't shift earlier ops
        pending.sort(key=lambda x: x[0], reverse=True)
        batch_requests = [req for _, req in pending]

        await retry_transient(
            lambda: docs_service.documents()
            .batchUpdate(documentId=file_id, body={"requests": batch_requests})
            .execute()
        )

    return {
        "file_id": file_id,
        "operations_applied": applied,
        "results": results,
    }
