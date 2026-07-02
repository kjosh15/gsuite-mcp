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
    # max_bytes=100 with 60-byte bodies: take_within_budget always includes unit 0
    # (60 bytes), then rejects unit 1 (60+60=120 > 100), so each page holds one
    # message. Verified by running the test.
    assert len(page1["messages"]) == 1

    seen = [m["id"] for m in page1["messages"]]
    cursor = page1["next_cursor"]
    truncated = page1["truncated"]
    last_page = page1
    while truncated:
        last_page = await gmail_ops.read_thread(svc, "t1", cursor=cursor, max_bytes=100)
        seen += [m["id"] for m in last_page["messages"]]
        cursor = last_page["next_cursor"]
        truncated = last_page["truncated"]

    assert seen == ["m0", "m1", "m2", "m3"]
    assert last_page["truncated"] is False


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


@pytest.mark.asyncio
async def test_invalid_offset_cursor_returns_error():
    svc = _service([_msg("m1", "a@x.com", "hi"), _msg("m2", "b@x.com", "yo")])
    # decodable cursor, valid version, but offset is out of range / wrong type
    bad_neg = pagination.encode_cursor({"kind": "thread", "offset": -1, "history_id": "100"})
    bad_type = pagination.encode_cursor({"kind": "thread", "offset": "nope", "history_id": "100"})
    assert (await gmail_ops.read_thread(svc, "t1", cursor=bad_neg))["error"] == "INVALID_CURSOR"
    assert (await gmail_ops.read_thread(svc, "t1", cursor=bad_type))["error"] == "INVALID_CURSOR"


@pytest.mark.asyncio
async def test_only_page_messages_are_processed(monkeypatch):
    # 4 messages, 60-byte bodies, budget 100 -> 1 message per page. Returning
    # page 1 must NOT decode/quote-strip the whole thread (avoids the O(N^2)
    # walk): only the returned message plus the one budget-probe message.
    msgs = [_msg(f"m{i}", "a@x.com", "x" * 60) for i in range(4)]
    svc = _service(msgs)

    calls = {"n": 0}
    real = gmail_ops._message_body

    def counting(payload, strip):
        calls["n"] += 1
        return real(payload, strip)

    monkeypatch.setattr(gmail_ops, "_message_body", counting)

    page1 = await gmail_ops.read_thread(svc, "t1", max_bytes=100)
    assert len(page1["messages"]) == 1
    assert calls["n"] == 2  # m0 (returned) + m1 (budget probe); never m2/m3


@pytest.mark.asyncio
async def test_oversized_first_message_still_returned():
    # Two 200-byte messages, tiny budget: page 1 must still hold exactly one
    # (forward progress) and report truncation.
    msgs = [_msg("m0", "a@x.com", "y" * 200), _msg("m1", "b@x.com", "z" * 200)]
    svc = _service(msgs)
    page1 = await gmail_ops.read_thread(svc, "t1", max_bytes=50)
    assert [m["id"] for m in page1["messages"]] == ["m0"]
    assert page1["truncated"] is True
