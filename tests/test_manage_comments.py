from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def mock_drive():
    with patch("gsuite_mcp.auth.get_drive_service") as mock:
        service = MagicMock()
        mock.return_value = service
        yield service


@pytest.mark.asyncio
async def test_manage_comments_list(mock_drive):
    mock_drive.comments().list.return_value.execute.return_value = {
        "comments": [
            {
                "id": "c1", "content": "first",
                "createdTime": "2026-04-01T10:00:00Z",
                "author": {"displayName": "Josh"},
                "resolved": False, "anchor": None,
                "replies": [
                    {"id": "r1", "content": "reply",
                     "createdTime": "2026-04-01T11:00:00Z",
                     "author": {"displayName": "Claude"}}
                ],
            }
        ]
    }

    from gsuite_mcp.server import manage_comments
    result = await manage_comments(file_id="f1", action="list")

    assert len(result["comments"]) == 1
    assert result["comments"][0]["comment_id"] == "c1"
    assert result["comments"][0]["replies"][0]["reply_id"] == "r1"


@pytest.mark.asyncio
async def test_manage_comments_create_unanchored(mock_drive):
    mock_drive.comments().create.return_value.execute.return_value = {
        "id": "new1", "content": "hi",
        "createdTime": "2026-04-10T12:00:00Z",
        "author": {"displayName": "Claude"},
    }
    from gsuite_mcp.server import manage_comments
    result = await manage_comments(file_id="f1", action="create", content="hi")
    assert result["comment_id"] == "new1"
    mock_drive.comments().create.assert_called()


@pytest.mark.asyncio
async def test_manage_comments_reply(mock_drive):
    mock_drive.replies().create.return_value.execute.return_value = {
        "id": "r9", "content": "ack",
        "createdTime": "2026-04-10T12:00:00Z",
        "author": {"displayName": "Claude"},
    }
    from gsuite_mcp.server import manage_comments
    result = await manage_comments(
        file_id="f1", action="reply", comment_id="c1", content="ack"
    )
    assert result["reply_id"] == "r9"


@pytest.mark.asyncio
async def test_manage_comments_resolve(mock_drive):
    mock_drive.replies().create.return_value.execute.return_value = {
        "id": "r1", "action": "resolve",
    }
    mock_drive.comments().get.return_value.execute.return_value = {
        "id": "c1", "content": "orig", "resolved": True,
    }
    from gsuite_mcp.server import manage_comments
    result = await manage_comments(
        file_id="f1", action="resolve", comment_id="c1"
    )
    assert result["resolved"] is True
    # Verify resolve was done via replies API
    mock_drive.replies().create.assert_called_once()
    call_kwargs = mock_drive.replies().create.call_args
    assert call_kwargs.kwargs["body"] == {"action": "resolve"}


@pytest.mark.asyncio
async def test_manage_comments_missing_required_param(mock_drive):
    from gsuite_mcp.server import manage_comments
    # reply without comment_id
    result = await manage_comments(file_id="f1", action="reply", content="hi")
    assert result["error"] == "MISSING_PARAM"
    # create without content
    result = await manage_comments(file_id="f1", action="create")
    assert result["error"] == "MISSING_PARAM"


@pytest.mark.asyncio
async def test_manage_comments_invalid_action(mock_drive):
    from gsuite_mcp.server import manage_comments
    result = await manage_comments(file_id="f1", action="nonsense")
    assert result["error"] == "INVALID_ACTION"
