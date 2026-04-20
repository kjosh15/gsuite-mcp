"""Gmail API operations — thread-aware draft creation."""

import asyncio
import base64
import re
from email.mime.text import MIMEText
from typing import Any, Optional


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
