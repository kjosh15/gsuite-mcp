"""create_reply_draft must never produce text/plain drafts.

Plain bodies land in Gmail hard-wrapped mid-sentence and look machine-drafted
(2026-10-19, 2026-11-03). Caller-side rules to pass content_type="html" failed
twice, so the server converts plain text to HTML and always sends text/html.
"""

import base64
import email
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gsuite_mcp.gmail_ops import _html_body, create_reply_draft

WRAPPED_BODY = (
    "Hi Maria,\n"
    "\n"
    "Thanks for sending the revised statement of work this morning, I have read\n"
    "it through and it looks right to me apart from two things.\n"
    "\n"
    "- The first milestone should move to the fifteenth of December so that it\n"
    "  lines up with the board meeting.\n"
    "- Please add the travel budget line we discussed on the call last week,\n"
    "  capped at the agreed amount.\n"
    "\n"
    "Kv.\n"
    "\n"
    "Josh"
)


def _gmail_service():
    svc = MagicMock()
    svc.users().messages().get.return_value.execute.return_value = {
        "payload": {"headers": [
            {"name": "Message-ID", "value": "<orig@mail.gmail.com>"},
            {"name": "Subject", "value": "SOW"},
        ]},
    }
    svc.users().drafts().create.return_value.execute.return_value = {
        "id": "d1", "message": {"id": "m1", "threadId": "t1"},
    }
    return svc


def _sent_message(svc):
    raw = svc.users().drafts().create.call_args.kwargs["body"]["message"]["raw"]
    return email.message_from_bytes(base64.urlsafe_b64decode(raw + "=="))


def test_wrapped_plain_body_unwraps_into_paragraphs_and_list():
    html = _html_body(WRAPPED_BODY, "plain")

    assert html == (
        "<p>Hi Maria,</p>"
        "<p>Thanks for sending the revised statement of work this morning, I have"
        " read it through and it looks right to me apart from two things.</p>"
        "<ul>"
        "<li>The first milestone should move to the fifteenth of December so that"
        " it lines up with the board meeting.</li>"
        "<li>Please add the travel budget line we discussed on the call last week,"
        " capped at the agreed amount.</li>"
        "</ul>"
        "<p>Kv.</p><p>Josh</p>"
    )


def test_short_line_break_becomes_br():
    assert _html_body("All best,\nJosh", "plain") == "<p>All best,<br>Josh</p>"


def test_body_with_html_tags_is_returned_unchanged_even_when_plain():
    assert _html_body("<p>Hi</p>", "plain") == "<p>Hi</p>"


def test_html_content_type_with_tags_is_returned_unchanged():
    assert _html_body("<div>Tom &amp; Jerry</div>", "html") == "<div>Tom &amp; Jerry</div>"


def test_tagless_body_is_converted_even_when_labelled_html():
    # The default content_type is "html", so a caller who omits it and sends
    # hard-wrapped plain text must still get converted, not passed through.
    assert _html_body("All best,\nJosh", "html") == "<p>All best,<br>Josh</p>"


def test_plain_text_is_html_escaped():
    assert _html_body("a < b & c", "plain") == "<p>a &lt; b &amp; c</p>"


@pytest.mark.asyncio
async def test_default_draft_is_text_html_with_converted_body():
    svc = _gmail_service()

    await create_reply_draft(
        gmail_service=svc, thread_id="t1", in_reply_to_message_id="m0",
        to="a@example.com", body="All best,\nJosh",
    )

    msg = _sent_message(svc)
    assert msg.get_content_type() == "text/html"
    assert msg.get_content_charset() == "utf-8"
    assert msg.get_payload(decode=True).decode("utf-8") == "<p>All best,<br>Josh</p>"


@pytest.mark.asyncio
async def test_explicit_plain_content_type_still_produces_text_html():
    svc = _gmail_service()

    await create_reply_draft(
        gmail_service=svc, thread_id="t1", in_reply_to_message_id="m0",
        to="a@example.com", body="Hi", content_type="plain",
    )

    assert _sent_message(svc).get_content_type() == "text/html"


@pytest.mark.asyncio
async def test_server_tool_defaults_content_type_to_html():
    from gsuite_mcp.server import create_reply_draft as tool

    with patch("gsuite_mcp.auth.get_gmail_service", return_value=MagicMock()), \
         patch("gsuite_mcp.gmail_ops.create_reply_draft", new_callable=AsyncMock) as m:
        await tool(thread_id="t1", in_reply_to_message_id="m0",
                   to="a@example.com", body="Hi")

    assert m.call_args.kwargs["content_type"] == "html"
