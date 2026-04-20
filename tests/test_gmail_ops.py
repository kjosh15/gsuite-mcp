"""Tests for gmail_ops.create_reply_draft."""

import base64
import email
from unittest.mock import MagicMock

import pytest


def _make_gmail_service(original_headers=None, draft_response=None):
    """Build a mock Gmail service with messages().get() and drafts().create()."""
    svc = MagicMock()

    # messages().get() → returns an original message with headers
    if original_headers is None:
        original_headers = [
            {"name": "Message-ID", "value": "<orig123@mail.gmail.com>"},
            {"name": "Subject", "value": "Q3 Planning"},
        ]
    msg_get = MagicMock()
    msg_get.execute.return_value = {
        "id": "msg_abc",
        "threadId": "thread_xyz",
        "payload": {"headers": original_headers},
    }
    svc.users().messages().get.return_value = msg_get

    # drafts().create() → returns a created draft
    if draft_response is None:
        draft_response = {
            "id": "draft_001",
            "message": {"id": "msg_draft_001", "threadId": "thread_xyz"},
        }
    draft_create = MagicMock()
    draft_create.execute.return_value = draft_response
    svc.users().drafts().create.return_value = draft_create

    return svc


@pytest.mark.asyncio
async def test_basic_reply_draft():
    """Happy path: creates a draft with correct threadId, In-Reply-To, subject."""
    svc = _make_gmail_service()

    from gsuite_mcp.gmail_ops import create_reply_draft

    result = await create_reply_draft(
        gmail_service=svc,
        thread_id="thread_xyz",
        in_reply_to_message_id="msg_abc",
        to="alice@example.com",
        body="Sounds good, let's proceed.",
    )

    assert result["draft_id"] == "draft_001"
    assert result["thread_id"] == "thread_xyz"
    assert result["subject"] == "Re: Q3 Planning"
    assert result["in_reply_to"] == "<orig123@mail.gmail.com>"

    # Verify the MIME message passed to drafts().create()
    call_kwargs = svc.users().drafts().create.call_args
    draft_body = call_kwargs.kwargs["body"]
    assert draft_body["message"]["threadId"] == "thread_xyz"
    raw = draft_body["message"]["raw"]
    mime_bytes = base64.urlsafe_b64decode(raw + "==")
    msg = email.message_from_bytes(mime_bytes)
    assert msg["In-Reply-To"] == "<orig123@mail.gmail.com>"
    assert msg["References"] == "<orig123@mail.gmail.com>"
    assert msg["Subject"] == "Re: Q3 Planning"
    assert msg["To"] == "alice@example.com"


@pytest.mark.asyncio
async def test_custom_subject_overrides_auto():
    """Providing an explicit subject skips auto Re: generation."""
    svc = _make_gmail_service()

    from gsuite_mcp.gmail_ops import create_reply_draft

    result = await create_reply_draft(
        gmail_service=svc,
        thread_id="thread_xyz",
        in_reply_to_message_id="msg_abc",
        to="bob@example.com",
        body="Custom topic.",
        subject="New Direction",
    )

    assert result["subject"] == "New Direction"

    call_kwargs = svc.users().drafts().create.call_args
    raw = call_kwargs.kwargs["body"]["message"]["raw"]
    mime_bytes = base64.urlsafe_b64decode(raw + "==")
    msg = email.message_from_bytes(mime_bytes)
    assert msg["Subject"] == "New Direction"


@pytest.mark.asyncio
async def test_no_double_re_prefix():
    """If original subject already has 'Re:', don't add another."""
    headers = [
        {"name": "Message-ID", "value": "<orig@mail.gmail.com>"},
        {"name": "Subject", "value": "Re: Weekly sync"},
    ]
    svc = _make_gmail_service(original_headers=headers)

    from gsuite_mcp.gmail_ops import create_reply_draft

    result = await create_reply_draft(
        gmail_service=svc,
        thread_id="t1",
        in_reply_to_message_id="m1",
        to="cc@example.com",
        body="Got it.",
    )

    assert result["subject"] == "Re: Weekly sync"


@pytest.mark.asyncio
async def test_cc_bcc_passed_through():
    """CC and BCC appear in the MIME headers."""
    svc = _make_gmail_service()

    from gsuite_mcp.gmail_ops import create_reply_draft

    await create_reply_draft(
        gmail_service=svc,
        thread_id="t1",
        in_reply_to_message_id="m1",
        to="a@example.com",
        body="FYI.",
        cc="b@example.com",
        bcc="c@example.com",
    )

    call_kwargs = svc.users().drafts().create.call_args
    raw = call_kwargs.kwargs["body"]["message"]["raw"]
    mime_bytes = base64.urlsafe_b64decode(raw + "==")
    msg = email.message_from_bytes(mime_bytes)
    assert msg["Cc"] == "b@example.com"
    assert msg["Bcc"] == "c@example.com"


@pytest.mark.asyncio
async def test_html_content_type():
    """content_type='html' produces text/html MIME."""
    svc = _make_gmail_service()

    from gsuite_mcp.gmail_ops import create_reply_draft

    await create_reply_draft(
        gmail_service=svc,
        thread_id="t1",
        in_reply_to_message_id="m1",
        to="a@example.com",
        body="<h1>Hello</h1>",
        content_type="html",
    )

    call_kwargs = svc.users().drafts().create.call_args
    raw = call_kwargs.kwargs["body"]["message"]["raw"]
    mime_bytes = base64.urlsafe_b64decode(raw + "==")
    msg = email.message_from_bytes(mime_bytes)
    assert msg.get_content_type() == "text/html"


@pytest.mark.asyncio
async def test_thread_id_in_draft_body():
    """threadId is set in the draft create request body."""
    svc = _make_gmail_service()

    from gsuite_mcp.gmail_ops import create_reply_draft

    await create_reply_draft(
        gmail_service=svc,
        thread_id="thread_custom",
        in_reply_to_message_id="m1",
        to="x@example.com",
        body="test",
    )

    call_kwargs = svc.users().drafts().create.call_args
    assert call_kwargs.kwargs["body"]["message"]["threadId"] == "thread_custom"
