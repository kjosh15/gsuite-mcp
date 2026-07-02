"""Gmail API operations — thread-aware draft creation and inbox delivery."""

import asyncio
import base64
import re
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Any, Optional

from gsuite_mcp import gmail_quotes, pagination


def _get_header(headers: list[dict], name: str) -> str:
    """Extract a header value from Gmail's payload.headers list."""
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _make_reply_subject(original_subject: str) -> str:
    """Add 'Re: ' prefix, avoiding duplication."""
    stripped = re.sub(r"^(Re:\s*)+", "", original_subject, flags=re.IGNORECASE)
    return f"Re: {stripped}"


async def create_reply_draft(
    gmail_service,
    thread_id: str,
    in_reply_to_message_id: str,
    to: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    subject: Optional[str] = None,
    content_type: str = "plain",
) -> dict[str, Any]:
    """Create a Gmail draft replying to a specific message in a thread.

    Args:
        gmail_service: Authenticated Gmail API service object.
        thread_id: Gmail thread ID to attach the draft to.
        in_reply_to_message_id: Gmail message ID of the message being replied to.
        to: Recipient email address.
        body: Draft body text (plain or HTML).
        cc: Optional CC recipients.
        bcc: Optional BCC recipients.
        subject: Optional subject override. Auto-generates 'Re: <original>' if omitted.
        content_type: 'plain' (default) or 'html'.

    Returns:
        dict with draft_id, message_id, thread_id, in_reply_to, subject, to,
        confirmation.
    """
    # 1. Fetch the original message to get RFC 2822 Message-ID and Subject
    original = await asyncio.to_thread(
        lambda: gmail_service.users()
        .messages()
        .get(userId="me", id=in_reply_to_message_id, format="metadata",
             metadataHeaders=["Message-ID", "Subject"])
        .execute()
    )
    headers = original.get("payload", {}).get("headers", [])
    rfc_message_id = _get_header(headers, "Message-ID")
    original_subject = _get_header(headers, "Subject")

    # 2. Determine subject
    if subject is None:
        subject = _make_reply_subject(original_subject)

    # 3. Build MIME message
    mime_subtype = "html" if content_type == "html" else "plain"
    msg = MIMEText(body, mime_subtype)
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    if rfc_message_id:
        msg["In-Reply-To"] = rfc_message_id
        msg["References"] = rfc_message_id

    # 4. Base64url encode
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    # 5. Create draft with threadId
    draft = await asyncio.to_thread(
        lambda: gmail_service.users()
        .drafts()
        .create(
            userId="me",
            body={"message": {"raw": raw, "threadId": thread_id}},
        )
        .execute()
    )

    return {
        "draft_id": draft["id"],
        "message_id": draft["message"]["id"],
        "thread_id": draft["message"]["threadId"],
        "in_reply_to": rfc_message_id,
        "subject": subject,
        "to": to,
        "confirmation": f"Draft created in thread {thread_id}",
    }


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
            start = pagination.offset_from(payload, len(messages))
        except ValueError:
            return {
                "error": "INVALID_CURSOR",
                "retryable": False,
                "message": "Cursor is malformed or unrecognized.",
            }
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


_SELF_ADDRESS = "josh@josh.is"


async def deliver_to_inbox(
    gmail_service,
    subject: str,
    body: str,
    content_type: str = "plain",
) -> dict[str, Any]:
    """Insert a message directly into the authenticated user's own inbox.

    Uses Gmail API users.messages.insert (NOT send). This method can only
    write to the authenticated user's mailbox — it cannot transmit to
    third parties.

    Args:
        gmail_service: Authenticated Gmail API service object.
        subject: Email subject line.
        body: Message body (plain text or HTML).
        content_type: 'plain' (default) or 'html'.

    Returns:
        dict with message_id and thread_id.
    """
    mime_subtype = "html" if content_type == "html" else "plain"
    msg = MIMEText(body, mime_subtype)
    msg["From"] = _SELF_ADDRESS
    msg["To"] = _SELF_ADDRESS
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    result = await asyncio.to_thread(
        lambda: gmail_service.users()
        .messages()
        .insert(
            userId="me",
            body={
                "raw": raw,
                "labelIds": ["INBOX", "UNREAD"],
            },
            internalDateSource="dateHeader",
        )
        .execute()
    )

    return {
        "message_id": result["id"],
        "thread_id": result["threadId"],
    }
