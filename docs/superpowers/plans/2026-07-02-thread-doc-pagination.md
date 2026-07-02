# Bounded Paginated Reads (read_thread + read_document) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two net-new read tools to gsuite-mcp — `read_thread` (Gmail) and `read_document` (Docs) — with never-silent truncation, cursor pagination, quote-stripping, and field projection.

**Architecture:** A shared `pagination.py` module owns the opaque cursor codec and a byte-budget windowing helper, so "never silent truncation" is one tested convention. `gmail_quotes.py` holds pure quote-stripping heuristics. `read_thread` lives in `gmail_ops.py`; `read_document` splits body pagination into `docs_ops.read_document_body` with comment projection orchestrated in the thin server tool (comments already live in `drive_ops`). Tools are registered in `server.py` following the existing `@mcp.tool()` wrapper pattern.

**Tech Stack:** Python 3.12+, FastMCP, google-api-python-client, pytest + pytest-asyncio, uv, ruff.

## Global Constraints

- No database, no state, no LLM calls — all reads are stateless; cursors are self-describing opaque tokens.
- Ops functions are `async def`, take the Google `service` object as the first arg, and use `asyncio.to_thread(lambda: ...execute())` for blocking API calls (see `gmail_ops.create_reply_draft`).
- Error returns are dicts shaped `{"error": "CODE", "retryable": bool, "message": str}` (see `docs_ops` COUNT_MISMATCH / HEADING_NOT_FOUND).
- File naming: `snake_case.py`; functions `snake_case`; constants `UPPER_SNAKE_CASE`.
- Fail fast at boundaries; use typed error codes; never swallow errors silently.
- Tests use `unittest.mock.MagicMock` for services and `@pytest.mark.asyncio` (see `tests/test_gmail_ops.py`).
- Run tests with `uv run pytest -q`; lint with `uv run ruff check .`.
- `max_bytes` default is `100_000`. Content is clipped only at whole-unit boundaries (whole message / whole structural element), never mid-unit; at least one unit is always emitted per page to guarantee forward progress.
- Cursors are base64url(JSON) with a version field; a malformed/unversioned cursor returns `INVALID_CURSOR`.

## File Structure

- Create `src/gsuite_mcp/pagination.py` — cursor codec + `take_within_budget`. Pure, no I/O.
- Create `src/gsuite_mcp/gmail_quotes.py` — `strip_quoted_history`, `html_to_text`. Pure.
- Modify `src/gsuite_mcp/gmail_ops.py` — add MIME body extraction helpers + `read_thread`.
- Modify `src/gsuite_mcp/docs_ops.py` — add `read_document_body`.
- Modify `src/gsuite_mcp/server.py` — register `read_thread` and `read_document` tools.
- Create `tests/test_pagination.py`, `tests/test_gmail_quotes.py`, `tests/test_read_thread.py`, `tests/test_read_document.py`.
- Modify `tests/test_server_snapshot_wiring.py` — assert the two new tools are registered.
- Modify `CLAUDE.md` — tool count 19 → 21, add tool entries, update test count.

---

### Task 1: Shared pagination module (`pagination.py`)

**Files:**
- Create: `src/gsuite_mcp/pagination.py`
- Test: `tests/test_pagination.py`

**Interfaces:**
- Produces:
  - `encode_cursor(payload: dict[str, Any]) -> str`
  - `decode_cursor(cursor: str) -> dict[str, Any]` — raises `ValueError` on malformed/unversioned input
  - `take_within_budget(sizes: list[int], start: int, max_bytes: int, hard_limit: int | None = None) -> int` — returns exclusive end index; always includes `sizes[start]` when `start < len(sizes)`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_pagination.py
"""Tests for the shared pagination helpers."""

import pytest

from gsuite_mcp import pagination


def test_cursor_roundtrip():
    token = pagination.encode_cursor({"kind": "thread", "offset": 3})
    payload = pagination.decode_cursor(token)
    assert payload["kind"] == "thread"
    assert payload["offset"] == 3
    assert payload["v"] == pagination.CURSOR_VERSION


def test_decode_malformed_raises():
    with pytest.raises(ValueError):
        pagination.decode_cursor("not-base64-!!!")


def test_decode_wrong_version_raises():
    import base64
    import json
    bad = base64.urlsafe_b64encode(json.dumps({"v": 999}).encode()).decode()
    with pytest.raises(ValueError):
        pagination.decode_cursor(bad)


def test_budget_stops_before_exceeding():
    # units of 40 bytes each, budget 100 -> take 2 (80), third would be 120
    assert pagination.take_within_budget([40, 40, 40, 40], 0, 100) == 2


def test_budget_always_takes_at_least_one():
    # single unit larger than budget is still emitted (forward progress)
    assert pagination.take_within_budget([500], 0, 100) == 1


def test_budget_respects_hard_limit():
    assert pagination.take_within_budget([10, 10, 10, 10], 0, 100, hard_limit=2) == 2


def test_budget_start_past_end():
    assert pagination.take_within_budget([10, 10], 5, 100) == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_pagination.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'gsuite_mcp.pagination'`

- [ ] **Step 3: Write the implementation**

```python
# src/gsuite_mcp/pagination.py
"""Shared pagination + truncation helpers for bounded reads.

Cursors are opaque base64url(JSON) tokens carrying a version and per-tool
position fields. Callers never parse them. Byte-budget windowing always
emits at least one unit so pagination cannot stall.
"""

import base64
import json
from typing import Any

CURSOR_VERSION = 1


def encode_cursor(payload: dict[str, Any]) -> str:
    """Serialize a cursor payload to an opaque base64url token."""
    raw = json.dumps({"v": CURSOR_VERSION, **payload}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> dict[str, Any]:
    """Decode an opaque cursor token. Raises ValueError if malformed."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed cursor: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise ValueError("unsupported cursor version")
    return payload


def take_within_budget(
    sizes: list[int],
    start: int,
    max_bytes: int,
    hard_limit: int | None = None,
) -> int:
    """Return the exclusive end index of units ``[start:end]`` fitting max_bytes.

    Always includes at least one unit (``sizes[start]``) when ``start`` is in
    range, guaranteeing forward progress even when a single unit exceeds the
    budget. ``hard_limit`` optionally caps the number of units in the window.
    """
    n = len(sizes)
    if start >= n:
        return start
    end = start
    total = 0
    while end < n:
        if hard_limit is not None and (end - start) >= hard_limit:
            break
        nxt = total + sizes[end]
        if end > start and nxt > max_bytes:
            break
        total = nxt
        end += 1
    return end
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_pagination.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add src/gsuite_mcp/pagination.py tests/test_pagination.py
git commit -m "feat: add shared pagination cursor codec + byte-budget helper"
```

---

### Task 2: Quote-stripping heuristics (`gmail_quotes.py`)

**Files:**
- Create: `src/gsuite_mcp/gmail_quotes.py`
- Test: `tests/test_gmail_quotes.py`

**Interfaces:**
- Produces:
  - `strip_quoted_history(text: str) -> tuple[str, bool]` — returns `(net_new_text, stripped)`; prefers keeping text
  - `html_to_text(html: str) -> str` — cuts at `gmail_quote` container, strips tags

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_gmail_quotes.py
"""Tests for quote-stripping heuristics."""

from gsuite_mcp import gmail_quotes


def test_strips_on_wrote_attribution():
    text = (
        "Sounds good, let's ship it.\n\n"
        "On Mon, Jul 1, 2026 at 3:04 PM Alice <a@x.com> wrote:\n"
        "> the original question\n"
    )
    net, stripped = gmail_quotes.strip_quoted_history(text)
    assert stripped is True
    assert net == "Sounds good, let's ship it."


def test_strips_original_message_separator():
    text = "My reply.\n\n-----Original Message-----\nFrom: Bob\nOlder stuff"
    net, stripped = gmail_quotes.strip_quoted_history(text)
    assert stripped is True
    assert net == "My reply."


def test_strips_bare_quote_block():
    text = "New content here.\n> quoted line one\n> quoted line two\n"
    net, stripped = gmail_quotes.strip_quoted_history(text)
    assert stripped is True
    assert net == "New content here."


def test_no_boundary_keeps_full_text():
    text = "Just a plain message with no quoting at all.\nSecond line."
    net, stripped = gmail_quotes.strip_quoted_history(text)
    assert stripped is False
    assert net == text


def test_all_quote_keeps_text_prefer_keep():
    # Stripping would leave nothing -> prefer keep (stripped=False)
    text = "> only quoted content\n> nothing new\n"
    net, stripped = gmail_quotes.strip_quoted_history(text)
    assert stripped is False
    assert net == text


def test_html_to_text_cuts_at_gmail_quote():
    html = '<div>Fresh reply</div><div class="gmail_quote">old quoted stuff</div>'
    assert gmail_quotes.html_to_text(html).strip() == "Fresh reply"


def test_html_to_text_strips_tags_and_entities():
    html = "<p>Hello&nbsp;&amp; welcome</p>"
    assert gmail_quotes.html_to_text(html).strip() == "Hello & welcome"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gmail_quotes.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'gsuite_mcp.gmail_quotes'`

- [ ] **Step 3: Write the implementation**

```python
# src/gsuite_mcp/gmail_quotes.py
"""Pure heuristics for stripping quoted history from email message bodies.

Detection is deliberately conservative: when no quote boundary is found — or
when stripping would leave only whitespace — the original text is returned
unchanged (prefer keeping net-new content over dropping it).
"""

import re

# Ordered earliest-match wins; each marks the start of quoted history.
_MARKERS = [
    re.compile(r"^On .+wrote:\s*$", re.MULTILINE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^-{3,}\s*Forwarded message\s*-{3,}\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^>", re.MULTILINE),
]

_GMAIL_QUOTE_DIV = re.compile(r'<div[^>]*class="[^"]*gmail_quote[^"]*"', re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_ENTITIES = (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'))


def html_to_text(html: str) -> str:
    """Minimal HTML→text: cut at the gmail_quote container, then strip tags."""
    m = _GMAIL_QUOTE_DIV.search(html)
    if m:
        html = html[: m.start()]
    text = _TAG.sub("", html)
    for ent, ch in _ENTITIES:
        text = text.replace(ent, ch)
    return text


def strip_quoted_history(text: str) -> tuple[str, bool]:
    """Return ``(net_new_text, stripped)``.

    Cuts at the earliest recognized quote boundary. Returns the original text
    with ``stripped=False`` when no boundary is found or when stripping would
    leave only whitespace.
    """
    earliest: int | None = None
    for marker in _MARKERS:
        m = marker.search(text)
        if m and (earliest is None or m.start() < earliest):
            earliest = m.start()
    if earliest is None:
        return text, False
    net_new = text[:earliest].rstrip()
    if not net_new.strip():
        return text, False
    return net_new, True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gmail_quotes.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add src/gsuite_mcp/gmail_quotes.py tests/test_gmail_quotes.py
git commit -m "feat: add pure quote-stripping + html-to-text heuristics"
```

---

### Task 3: `read_thread` (Gmail)

**Files:**
- Modify: `src/gsuite_mcp/gmail_ops.py` (add helpers + `read_thread` after `create_reply_draft`)
- Test: `tests/test_read_thread.py`

**Interfaces:**
- Consumes: `pagination.decode_cursor`, `pagination.encode_cursor`, `pagination.take_within_budget`; `gmail_quotes.strip_quoted_history`, `gmail_quotes.html_to_text`; existing `_get_header`.
- Produces:
  - `read_thread(gmail_service, thread_id: str, strip_quoted_history: bool = False, message_limit: int | None = None, cursor: str | None = None, max_bytes: int = 100_000) -> dict[str, Any]`
  - Response keys: `thread_id`, `messages` (list of `{id, from, to, date, subject, body, quoted_history_stripped}`), `truncated`, `next_cursor`, `thread_changed`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_read_thread.py
"""Tests for gmail_ops.read_thread."""

import base64
from unittest.mock import MagicMock

import pytest

from gsuite_mcp import gmail_ops, pagination


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _msg(mid, sender, body, history="100"):
    return {
        "id": mid,
        "historyId": history,
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": "me@x.com"},
                {"name": "Date", "value": "Mon, 1 Jul 2026 10:00:00 -0700"},
                {"name": "Subject", "value": "Planning"},
            ],
            "body": {"data": _b64(body)},
        },
    }


def _service(messages, history_id="100"):
    svc = MagicMock()
    get = MagicMock()
    get.execute.return_value = {"id": "t1", "historyId": history_id, "messages": messages}
    svc.users().threads().get.return_value = get
    return svc


@pytest.mark.asyncio
async def test_reads_all_messages_single_page():
    svc = _service([_msg("m1", "a@x.com", "hello"), _msg("m2", "b@x.com", "world")])
    result = await gmail_ops.read_thread(svc, "t1")
    assert [m["id"] for m in result["messages"]] == ["m1", "m2"]
    assert result["truncated"] is False
    assert result["next_cursor"] is None
    assert result["messages"][0]["from"] == "a@x.com"


@pytest.mark.asyncio
async def test_paginates_and_resumes_via_cursor():
    msgs = [_msg(f"m{i}", "a@x.com", "x" * 60) for i in range(4)]
    svc = _service(msgs)
    page1 = await gmail_ops.read_thread(svc, "t1", max_bytes=100)
    assert page1["truncated"] is True
    assert len(page1["messages"]) == 2  # 60+60=120 > 100 -> stop at 2? see budget rule
    page2 = await gmail_ops.read_thread(svc, "t1", cursor=page1["next_cursor"], max_bytes=100)
    seen = [m["id"] for m in page1["messages"]] + [m["id"] for m in page2["messages"]]
    assert seen == ["m0", "m1", "m2", "m3"]
    assert page2["truncated"] is False


@pytest.mark.asyncio
async def test_message_limit_caps_page():
    msgs = [_msg(f"m{i}", "a@x.com", "short") for i in range(5)]
    svc = _service(msgs)
    result = await gmail_ops.read_thread(svc, "t1", message_limit=2)
    assert len(result["messages"]) == 2
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_strip_quoted_history_flag():
    body = "New reply.\n\nOn Mon, Jul 1 2026 Alice wrote:\n> old\n"
    svc = _service([_msg("m1", "a@x.com", body)])
    result = await gmail_ops.read_thread(svc, "t1", strip_quoted_history=True)
    assert result["messages"][0]["body"] == "New reply."
    assert result["messages"][0]["quoted_history_stripped"] is True


@pytest.mark.asyncio
async def test_thread_changed_flag_on_new_history():
    svc = _service([_msg("m1", "a@x.com", "hi")], history_id="200")
    stale = pagination.encode_cursor({"kind": "thread", "offset": 0, "history_id": "100"})
    result = await gmail_ops.read_thread(svc, "t1", cursor=stale)
    assert result["thread_changed"] is True


@pytest.mark.asyncio
async def test_malformed_cursor_returns_error():
    svc = _service([_msg("m1", "a@x.com", "hi")])
    result = await gmail_ops.read_thread(svc, "t1", cursor="garbage!!!")
    assert result["error"] == "INVALID_CURSOR"
```

Note on `test_paginates_and_resumes_via_cursor`: with `max_bytes=100` and 60-byte units, `take_within_budget` takes unit 0 (60), then 60+60=120 > 100 so stops → 1 message per page. Adjust the assertion to `== 1` if the budget rule yields 1. **Before finalizing, run the test and set the expected page size to what `take_within_budget` actually returns** (unit 0 always included; unit 1 rejected because 120 > 100 → expect 1 message on page 1, and pagination continues one message per page). Update the assert to `== 1` and keep the `seen == [...]` full-walk assertion (which is the real acceptance check).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_read_thread.py -q`
Expected: FAIL with `AttributeError: module 'gsuite_mcp.gmail_ops' has no attribute 'read_thread'`

- [ ] **Step 3: Add the imports and helpers to `gmail_ops.py`**

At the top of `src/gsuite_mcp/gmail_ops.py`, add to the imports:

```python
from gsuite_mcp import gmail_quotes, pagination
```

Add these helpers and `read_thread` after `create_reply_draft` (before `_SELF_ADDRESS`):

```python
def _decode_part(data: str) -> str:
    """Base64url-decode a Gmail message part body, tolerating missing padding."""
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")


def _extract_plain_and_html(payload: dict) -> tuple[str, str]:
    """Walk a message payload's MIME tree; return (plain_text, html_text)."""
    mime = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data", "")
    if mime == "text/plain":
        return _decode_part(data), ""
    if mime == "text/html":
        return "", _decode_part(data)
    plain, html = "", ""
    for part in payload.get("parts", []):
        p, h = _extract_plain_and_html(part)
        plain = plain or p
        html = html or h
    return plain, html


def _message_body(payload: dict, strip_quotes: bool) -> tuple[str, bool]:
    """Return (body_text, quoted_history_stripped) for one message payload."""
    plain, html = _extract_plain_and_html(payload)
    text = plain if plain else gmail_quotes.html_to_text(html)
    if strip_quotes:
        return gmail_quotes.strip_quoted_history(text)
    return text, False


async def read_thread(
    gmail_service,
    thread_id: str,
    strip_quoted_history: bool = False,
    message_limit: int | None = None,
    cursor: str | None = None,
    max_bytes: int = 100_000,
) -> dict[str, Any]:
    """Read a Gmail thread with bounded, cursor-based pagination.

    Threads are append-only: existing messages never change, so an offset
    cursor stays valid. If new messages arrived since the cursor was issued
    the response sets ``thread_changed: true`` and continues.

    Args:
        gmail_service: Authenticated Gmail API service object.
        thread_id: Gmail thread ID.
        strip_quoted_history: When True, return only each message's net-new
            body text; each message reports ``quoted_history_stripped``.
        message_limit: Optional max messages per page (in addition to max_bytes).
        cursor: Opaque page token from a prior call.
        max_bytes: Response body size budget (default 100_000).

    Returns:
        dict with thread_id, messages, truncated, next_cursor, thread_changed;
        or an INVALID_CURSOR error dict.
    """
    thread = await asyncio.to_thread(
        lambda: gmail_service.users()
        .threads()
        .get(userId="me", id=thread_id, format="full")
        .execute()
    )
    messages = thread.get("messages", [])
    history_id = thread.get("historyId", "")

    start = 0
    prev_history = None
    if cursor is not None:
        try:
            payload = pagination.decode_cursor(cursor)
        except ValueError:
            return {
                "error": "INVALID_CURSOR",
                "retryable": False,
                "message": "Cursor is malformed or unrecognized.",
            }
        start = int(payload.get("offset", 0))
        prev_history = payload.get("history_id")

    built: list[dict[str, Any]] = []
    sizes: list[int] = []
    for msg in messages:
        payload_ = msg.get("payload", {})
        headers = payload_.get("headers", [])
        body_text, stripped = _message_body(payload_, strip_quoted_history)
        built.append({
            "id": msg.get("id", ""),
            "from": _get_header(headers, "From"),
            "to": _get_header(headers, "To"),
            "date": _get_header(headers, "Date"),
            "subject": _get_header(headers, "Subject"),
            "body": body_text,
            "quoted_history_stripped": stripped,
        })
        sizes.append(len(body_text.encode("utf-8")))

    limit = message_limit if (message_limit is None or message_limit >= 1) else 1
    end = pagination.take_within_budget(sizes, start, max_bytes, hard_limit=limit)
    truncated = end < len(built)
    next_cursor = (
        pagination.encode_cursor(
            {"kind": "thread", "offset": end, "history_id": history_id}
        )
        if truncated
        else None
    )

    return {
        "thread_id": thread_id,
        "messages": built[start:end],
        "truncated": truncated,
        "next_cursor": next_cursor,
        "thread_changed": prev_history is not None and prev_history != history_id,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_read_thread.py -q`
Expected: PASS (adjust the page-size assert in `test_paginates_and_resumes_via_cursor` per the note in Step 1, then all pass)

- [ ] **Step 5: Commit**

```bash
git add src/gsuite_mcp/gmail_ops.py tests/test_read_thread.py
git commit -m "feat: add read_thread with quote-stripping and message pagination"
```

---

### Task 4: `read_document_body` (Docs body pagination)

**Files:**
- Modify: `src/gsuite_mcp/docs_ops.py` (add `read_document_body` at end of file)
- Test: `tests/test_read_document.py` (body tests here; tool-level projection tested in Task 5)

**Interfaces:**
- Consumes: `pagination.decode_cursor`, `pagination.encode_cursor`, `pagination.take_within_budget`; existing `_para_text`.
- Produces:
  - `read_document_body(docs_service, file_id: str, cursor: str | None = None, max_bytes: int = 100_000) -> dict[str, Any]`
  - Success keys: `revision_id`, `body`, `truncated`, `next_cursor`.
  - Error returns: `INVALID_CURSOR`, `STALE_CURSOR` (includes current `revision_id`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_read_document.py
"""Tests for docs_ops.read_document_body."""

from unittest.mock import MagicMock

import pytest

from gsuite_mcp import docs_ops, pagination


def _para(text, start):
    return {
        "startIndex": start,
        "endIndex": start + len(text),
        "paragraph": {"elements": [{"textRun": {"content": text}}]},
    }


def _doc(paras, revision="rev1"):
    content = []
    idx = 1
    for p in paras:
        content.append(_para(p, idx))
        idx += len(p)
    return {"revisionId": revision, "body": {"content": content}}


def _service(doc):
    svc = MagicMock()
    get = MagicMock()
    get.execute.return_value = doc
    svc.documents().get.return_value = get
    return svc


@pytest.mark.asyncio
async def test_reads_full_body_single_page():
    svc = _service(_doc(["Alpha\n", "Beta\n"]))
    result = await docs_ops.read_document_body(svc, "d1")
    assert result["body"] == "Alpha\nBeta\n"
    assert result["truncated"] is False
    assert result["next_cursor"] is None
    assert result["revision_id"] == "rev1"


@pytest.mark.asyncio
async def test_paginates_body_and_resumes():
    svc = _service(_doc(["A" * 60 + "\n", "B" * 60 + "\n"]))
    page1 = await docs_ops.read_document_body(svc, "d1", max_bytes=100)
    assert page1["truncated"] is True
    page2 = await docs_ops.read_document_body(svc, "d1", cursor=page1["next_cursor"], max_bytes=100)
    assert page1["body"] + page2["body"] == "A" * 60 + "\n" + "B" * 60 + "\n"
    assert page2["truncated"] is False


@pytest.mark.asyncio
async def test_stale_cursor_when_revision_changes():
    svc = _service(_doc(["Alpha\n", "Beta\n"], revision="rev2"))
    stale = pagination.encode_cursor({"kind": "doc", "offset": 1, "revision_id": "rev1"})
    result = await docs_ops.read_document_body(svc, "d1", cursor=stale)
    assert result["error"] == "STALE_CURSOR"
    assert result["revision_id"] == "rev2"


@pytest.mark.asyncio
async def test_malformed_cursor_returns_error():
    svc = _service(_doc(["Alpha\n"]))
    result = await docs_ops.read_document_body(svc, "d1", cursor="garbage!!!")
    assert result["error"] == "INVALID_CURSOR"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_read_document.py -q`
Expected: FAIL with `AttributeError: module 'gsuite_mcp.docs_ops' has no attribute 'read_document_body'`

- [ ] **Step 3: Add the implementation to `docs_ops.py`**

At the top of `src/gsuite_mcp/docs_ops.py`, add to the imports:

```python
from gsuite_mcp import pagination
```

Append to the end of `src/gsuite_mcp/docs_ops.py`:

```python
# ---------------------------------------------------------------------------
# Bounded body read
# ---------------------------------------------------------------------------


async def read_document_body(
    docs_service,
    file_id: str,
    cursor: str | None = None,
    max_bytes: int = 100_000,
) -> dict[str, Any]:
    """Read a Google Doc's body text with bounded, cursor-based pagination.

    Pagination is by structural element (content block). The cursor embeds the
    document ``revisionId``; if the doc changed since the cursor was issued the
    call returns a ``STALE_CURSOR`` error (indices may have shifted) rather than
    risk skipping or duplicating content.

    Returns success keys revision_id, body, truncated, next_cursor; or an
    INVALID_CURSOR / STALE_CURSOR error dict.
    """
    doc = await asyncio.to_thread(
        lambda: docs_service.documents().get(documentId=file_id).execute()
    )
    revision_id = doc.get("revisionId", "")
    content = doc.get("body", {}).get("content", [])

    unit_texts = [
        _para_text(block["paragraph"]) if block.get("paragraph") else ""
        for block in content
    ]

    start = 0
    if cursor is not None:
        try:
            payload = pagination.decode_cursor(cursor)
        except ValueError:
            return {
                "error": "INVALID_CURSOR",
                "retryable": False,
                "message": "Cursor is malformed or unrecognized.",
            }
        if payload.get("revision_id") != revision_id:
            return {
                "error": "STALE_CURSOR",
                "retryable": True,
                "revision_id": revision_id,
                "message": (
                    "Document changed since the cursor was issued. "
                    "Restart pagination from the beginning."
                ),
            }
        start = int(payload.get("offset", 0))

    sizes = [len(t.encode("utf-8")) for t in unit_texts]
    end = pagination.take_within_budget(sizes, start, max_bytes)
    truncated = end < len(unit_texts)
    next_cursor = (
        pagination.encode_cursor(
            {"kind": "doc", "offset": end, "revision_id": revision_id}
        )
        if truncated
        else None
    )

    return {
        "revision_id": revision_id,
        "body": "".join(unit_texts[start:end]),
        "truncated": truncated,
        "next_cursor": next_cursor,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_read_document.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/gsuite_mcp/docs_ops.py tests/test_read_document.py
git commit -m "feat: add read_document_body with STALE_CURSOR guard and pagination"
```

---

### Task 5: Register `read_thread` and `read_document` tools in `server.py`

**Files:**
- Modify: `src/gsuite_mcp/server.py` (add two `@mcp.tool()` functions after `read_paragraph_at_path`, before `def main()`)
- Modify: `tests/test_server_snapshot_wiring.py`
- Test additions: `tests/test_read_document.py` (tool-level projection)

**Interfaces:**
- Consumes: `gmail_ops.read_thread`, `docs_ops.read_document_body`, `drive_ops.list_comments`, `auth.get_gmail_service`, `auth.get_docs_service`, `auth.get_drive_service`, existing `GOOGLE_DOC_MIME`.
- Produces MCP tools:
  - `read_thread(thread_id, strip_quoted_history=False, message_limit=None, cursor=None, max_bytes=100000)`
  - `read_document(file_id, fields=None, cursor=None, max_bytes=100000)` — `fields` subset of `["body","comments"]`; omit = both; `NOT_A_GOOGLE_DOC` / `INVALID_FIELDS` errors; passes through `trashed`/`trashed_time`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_read_document.py`:

```python
@pytest.mark.asyncio
async def test_tool_read_document_body_only(monkeypatch):
    from gsuite_mcp import server

    monkeypatch.setattr(server.auth, "get_drive_service", lambda: _meta_service("application/vnd.google-apps.document"))
    monkeypatch.setattr(server.auth, "get_docs_service", lambda: _service(_doc(["Body text\n"])))

    result = await server.read_document.fn(file_id="d1", fields=["body"])
    assert result["body"] == "Body text\n"
    assert "comments" not in result


@pytest.mark.asyncio
async def test_tool_read_document_rejects_non_doc(monkeypatch):
    from gsuite_mcp import server

    monkeypatch.setattr(server.auth, "get_drive_service", lambda: _meta_service("application/pdf"))
    result = await server.read_document.fn(file_id="d1")
    assert result["error"] == "NOT_A_GOOGLE_DOC"


@pytest.mark.asyncio
async def test_tool_read_document_invalid_fields(monkeypatch):
    from gsuite_mcp import server

    monkeypatch.setattr(server.auth, "get_drive_service", lambda: _meta_service("application/vnd.google-apps.document"))
    result = await server.read_document.fn(file_id="d1", fields=["bogus"])
    assert result["error"] == "INVALID_FIELDS"


def _meta_service(mime, trashed=False):
    svc = MagicMock()
    get = MagicMock()
    get.execute.return_value = {"mimeType": mime, "trashed": trashed, "trashedTime": None}
    svc.files().get.return_value = get
    return svc
```

> Note: FastMCP wraps tool functions; call the underlying coroutine via `.fn(...)` (used by existing wiring tests). If the local FastMCP version exposes it differently, check `tests/test_server_snapshot_wiring.py` for the accessor already in use and match it.

Add to `tests/test_server_snapshot_wiring.py` (follow the file's existing assertion style for registered tool names):

```python
def test_read_tools_registered():
    from gsuite_mcp import server
    names = {t.name for t in server.mcp._tool_manager.list_tools()}  # match existing accessor
    assert "read_thread" in names
    assert "read_document" in names
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_read_document.py tests/test_server_snapshot_wiring.py -q`
Expected: FAIL — `read_document` / `read_thread` not registered.

- [ ] **Step 3: Add the tools to `server.py`**

Insert after `read_paragraph_at_path` (before `def main()`):

```python
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

    if want_comments:
        comments = await drive_ops.list_comments(drive, file_id, include_resolved=True)
        result["comments"] = comments["comments"]

    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_read_document.py tests/test_server_snapshot_wiring.py -q`
Expected: PASS

- [ ] **Step 5: Run the full suite + lint**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass, no lint errors. Record the total passed count for the docs update.

- [ ] **Step 6: Commit**

```bash
git add src/gsuite_mcp/server.py tests/test_read_document.py tests/test_server_snapshot_wiring.py
git commit -m "feat: register read_thread and read_document MCP tools"
```

---

### Task 6: Documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the tool list and structure notes**

In `CLAUDE.md`:
- Change `server.py — FastMCP server exposing 19 tools` → `21 tools`.
- Update `gmail_ops.py` bullet to mention `read_thread` (thread-aware read with quote-stripping + pagination).
- Update `docs_ops.py` bullet to mention `read_document_body`.
- Add a bullet: `src/gsuite_mcp/pagination.py — opaque cursor codec + byte-budget windowing (pure functions)`.
- Add a bullet: `src/gsuite_mcp/gmail_quotes.py — quoted-history stripping + html-to-text (pure functions)`.
- Add tools 20 and 21 to the Tools list:
  - `20. read_thread — bounded Gmail thread read: strip_quoted_history, message_limit/cursor pagination, never-silent truncation (truncated + next_cursor), thread_changed flag on append.`
  - `21. read_document — bounded Google Doc read: fields projection (["body"]/["comments"]/both), structural-element pagination, STALE_CURSOR on mid-pagination edits.`
- Update the test count `(292 tests)` to the number reported by `uv run pytest -q` in Task 5.
- Under Key Constraints, add: `read_thread/read_document never truncate silently — every response carries truncated + next_cursor; Docs pagination returns STALE_CURSOR when the doc changes mid-read, Gmail sets thread_changed and continues (threads are append-only).`
- Under Key Constraints, add the v1 scope note: `read_document comment projection returns comments via the Drive comments API (first page, up to ~100); comment-level pagination is out of scope for v1.`

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document read_thread and read_document tools"
```

---

## Self-Review

**1. Spec coverage:**
- 50-message thread fully readable → Task 3 (`read_thread` offset pagination + `next_cursor`). ✅
- Multi-page doc fully readable → Task 4 (`read_document_body` structural-element pagination). ✅
- Truncation always explicit → Tasks 3 & 4 both return `truncated` + `next_cursor`; `take_within_budget` guarantees progress (Task 1). ✅
- Net-new body with quoted history stripped + per-message flag → Task 2 + Task 3 (`quoted_history_stripped`). ✅
- `read_document` field projection (body without comments) → Task 5. ✅
- Docs `STALE_CURSOR`; Gmail `thread_changed` → Task 4 (STALE_CURSOR) + Task 3 (thread_changed). ✅
- Comment pagination is explicitly out of scope for v1 → noted in Task 6 constraint and spec "Out of scope". ✅

**2. Placeholder scan:** No TBD/TODO; the one deferred value (final test count) is a mechanical read from `uv run pytest -q` in Task 5, applied in Task 6. The page-size assert note in Task 3 gives the exact rule and the resolving action.

**3. Type consistency:** `take_within_budget(sizes, start, max_bytes, hard_limit)` signature matches all call sites (Tasks 3, 4). Cursor payload keys are consistent: threads use `{kind, offset, history_id}`, docs use `{kind, offset, revision_id}`. `read_document_body` return keys (`revision_id, body, truncated, next_cursor`) match what the server tool spreads via `result.update(body)`. `_message_body` returns `(str, bool)` consumed as `body_text, stripped`.
