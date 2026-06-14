"""Tests for the deliver_to_inbox MCP tool in server.py."""

from unittest.mock import patch, MagicMock, AsyncMock

import pytest


@pytest.fixture
def mock_gmail():
    with patch("gsuite_mcp.auth.get_gmail_service") as mock:
        service = MagicMock()
        mock.return_value = service
        yield service


@pytest.fixture
def mock_deliver_to_inbox():
    with patch("gsuite_mcp.gmail_ops.deliver_to_inbox", new_callable=AsyncMock) as m:
        m.return_value = {
            "message_id": "msg_001",
            "thread_id": "thread_001",
        }
        yield m


@pytest.mark.asyncio
async def test_deliver_to_inbox_happy_path(mock_gmail, mock_deliver_to_inbox):
    """Tool delegates to gmail_ops.deliver_to_inbox with correct args."""
    from gsuite_mcp.server import deliver_to_inbox

    result = await deliver_to_inbox(
        subject="Test",
        body="Hello",
    )

    assert result["message_id"] == "msg_001"
    assert result["thread_id"] == "thread_001"
    mock_deliver_to_inbox.assert_awaited_once()
    call_kwargs = mock_deliver_to_inbox.call_args.kwargs
    assert call_kwargs["subject"] == "Test"
    assert call_kwargs["body"] == "Hello"
    assert call_kwargs["content_type"] == "plain"


@pytest.mark.asyncio
async def test_deliver_to_inbox_html(mock_gmail, mock_deliver_to_inbox):
    """content_type='html' passes through."""
    from gsuite_mcp.server import deliver_to_inbox

    await deliver_to_inbox(
        subject="HTML",
        body="<p>Hi</p>",
        content_type="html",
    )

    call_kwargs = mock_deliver_to_inbox.call_args.kwargs
    assert call_kwargs["content_type"] == "html"


@pytest.mark.asyncio
async def test_deliver_to_inbox_api_error_propagates(mock_gmail):
    """API errors bubble up."""
    with patch(
        "gsuite_mcp.gmail_ops.deliver_to_inbox",
        new_callable=AsyncMock,
        side_effect=RuntimeError("Gmail API error"),
    ):
        from gsuite_mcp.server import deliver_to_inbox

        with pytest.raises(RuntimeError, match="Gmail API error"):
            await deliver_to_inbox(subject="Test", body="test")
