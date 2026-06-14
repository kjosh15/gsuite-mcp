"""Tests for gmail_ops.deliver_to_inbox."""

import base64
import email
from unittest.mock import MagicMock

import pytest


def _make_gmail_service(insert_response=None):
    """Build a mock Gmail service with messages().insert()."""
    svc = MagicMock()

    if insert_response is None:
        insert_response = {
            "id": "msg_inserted_001",
            "threadId": "thread_inserted_001",
            "labelIds": ["INBOX", "UNREAD"],
        }
    msg_insert = MagicMock()
    msg_insert.execute.return_value = insert_response
    svc.users().messages().insert.return_value = msg_insert

    return svc


@pytest.mark.asyncio
async def test_basic_deliver_to_inbox():
    """Happy path: inserts message with correct MIME structure."""
    svc = _make_gmail_service()

    from gsuite_mcp.gmail_ops import deliver_to_inbox

    result = await deliver_to_inbox(
        gmail_service=svc,
        subject="Test note",
        body="Hello from Claude",
    )

    assert result["message_id"] == "msg_inserted_001"
    assert result["thread_id"] == "thread_inserted_001"

    # Verify the API call
    call_kwargs = svc.users().messages().insert.call_args
    assert call_kwargs.kwargs["userId"] == "me"
    assert call_kwargs.kwargs["internalDateSource"] == "dateHeader"

    # Verify label IDs
    body = call_kwargs.kwargs["body"]
    assert "INBOX" in body["labelIds"]
    assert "UNREAD" in body["labelIds"]

    # Verify MIME message
    raw = body["raw"]
    mime_bytes = base64.urlsafe_b64decode(raw + "==")
    msg = email.message_from_bytes(mime_bytes)
    assert msg["From"] == "josh@josh.is"
    assert msg["To"] == "josh@josh.is"
    assert msg["Subject"] == "Test note"
    assert msg.get_content_type() == "text/plain"
    assert "Hello from Claude" in msg.get_payload(decode=True).decode()


@pytest.mark.asyncio
async def test_html_content_type():
    """content_type='html' produces text/html MIME."""
    svc = _make_gmail_service()

    from gsuite_mcp.gmail_ops import deliver_to_inbox

    await deliver_to_inbox(
        gmail_service=svc,
        subject="HTML note",
        body="<h1>Hello</h1>",
        content_type="html",
    )

    call_kwargs = svc.users().messages().insert.call_args
    raw = call_kwargs.kwargs["body"]["raw"]
    mime_bytes = base64.urlsafe_b64decode(raw + "==")
    msg = email.message_from_bytes(mime_bytes)
    assert msg.get_content_type() == "text/html"


@pytest.mark.asyncio
async def test_date_header_present():
    """MIME message includes a Date header."""
    svc = _make_gmail_service()

    from gsuite_mcp.gmail_ops import deliver_to_inbox

    await deliver_to_inbox(
        gmail_service=svc,
        subject="Dated note",
        body="test",
    )

    call_kwargs = svc.users().messages().insert.call_args
    raw = call_kwargs.kwargs["body"]["raw"]
    mime_bytes = base64.urlsafe_b64decode(raw + "==")
    msg = email.message_from_bytes(mime_bytes)
    assert msg["Date"] is not None


@pytest.mark.asyncio
async def test_no_recipient_argument():
    """deliver_to_inbox does not accept a 'to' parameter — structurally single-recipient."""
    import inspect
    from gsuite_mcp.gmail_ops import deliver_to_inbox

    sig = inspect.signature(deliver_to_inbox)
    param_names = set(sig.parameters.keys())
    assert "to" not in param_names
    assert "cc" not in param_names
    assert "bcc" not in param_names
