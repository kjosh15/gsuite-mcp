from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def mock_services():
    with patch("gsuite_mcp.auth.get_drive_service") as mock_drive, \
         patch("gsuite_mcp.auth.get_docs_service") as mock_docs:
        drive = MagicMock()
        docs = MagicMock()
        mock_drive.return_value = drive
        mock_docs.return_value = docs
        yield {"drive": drive, "docs": docs}


@pytest.mark.asyncio
async def test_template_populate_copies_and_replaces(mock_services):
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().copy.return_value.execute.return_value = {
        "id": "new123",
        "name": "My New Doc",
        "webViewLink": "https://docs.google.com/document/d/new123/edit",
    }
    docs.documents().batchUpdate.return_value.execute.return_value = {
        "replies": [
            {"replaceAllText": {"occurrencesChanged": 1}},
            {"replaceAllText": {"occurrencesChanged": 2}},
        ]
    }

    from gsuite_mcp.server import gdoc_template_populate
    result = await gdoc_template_populate(
        template_file_id="tmpl1",
        parent_folder_id="folder1",
        new_title="My New Doc",
        replacements={"{{NAME}}": "Alice", "{{DATE}}": "2026-05-05"},
    )

    assert result["file_id"] == "new123"
    assert result["web_view_link"] == "https://docs.google.com/document/d/new123/edit"
    assert result["replacements_made"] == {"{{NAME}}": 1, "{{DATE}}": 2}

    copy_call = drive.files().copy.call_args
    assert copy_call.kwargs["fileId"] == "tmpl1"
    body = copy_call.kwargs["body"]
    assert body["name"] == "My New Doc"
    assert body["parents"] == ["folder1"]
    assert body["mimeType"] == "application/vnd.google-apps.document"

    batch_call = docs.documents().batchUpdate.call_args
    requests = batch_call.kwargs["body"]["requests"]
    assert len(requests) == 2
    assert requests[0]["replaceAllText"]["containsText"]["text"] == "{{NAME}}"
    assert requests[0]["replaceAllText"]["replaceText"] == "Alice"


@pytest.mark.asyncio
async def test_template_populate_empty_replacements(mock_services):
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().copy.return_value.execute.return_value = {
        "id": "new456",
        "name": "Empty",
        "webViewLink": "https://docs.google.com/document/d/new456/edit",
    }

    from gsuite_mcp.server import gdoc_template_populate
    result = await gdoc_template_populate(
        template_file_id="tmpl1",
        parent_folder_id="folder1",
        new_title="Empty",
        replacements={},
    )

    assert result["file_id"] == "new456"
    assert result["replacements_made"] == {}
    # batchUpdate should NOT be called with empty replacements
    docs.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_template_populate_zero_occurrences(mock_services):
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().copy.return_value.execute.return_value = {
        "id": "new789",
        "name": "NoMatch",
        "webViewLink": "https://docs.google.com/document/d/new789/edit",
    }
    docs.documents().batchUpdate.return_value.execute.return_value = {
        "replies": [{"replaceAllText": {}}]  # no occurrencesChanged key
    }

    from gsuite_mcp.server import gdoc_template_populate
    result = await gdoc_template_populate(
        template_file_id="tmpl1",
        parent_folder_id="folder1",
        new_title="NoMatch",
        replacements={"{{MISSING}}": "value"},
    )

    assert result["replacements_made"] == {"{{MISSING}}": 0}


@pytest.mark.asyncio
async def test_template_populate_with_post_styles(mock_services):
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().copy.return_value.execute.return_value = {
        "id": "new_styled",
        "name": "Styled Doc",
        "webViewLink": "https://docs.google.com/document/d/new_styled/edit",
    }
    docs.documents().batchUpdate.return_value.execute.return_value = {
        "replies": [
            {"replaceAllText": {"occurrencesChanged": 1}},
        ]
    }
    # format_document needs documents().get() for doc structure
    docs.documents().get.return_value.execute.return_value = {
        "body": {"content": [
            {
                "startIndex": 0, "endIndex": 20,
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "elements": [{"startIndex": 0, "endIndex": 20,
                                  "textRun": {"content": "Executive Summary\n"}}],
                },
            },
        ]}
    }

    from gsuite_mcp.gdoc_ops import template_populate
    result = await template_populate(
        drive_service=drive,
        docs_service=docs,
        template_file_id="tmpl1",
        parent_folder_id="folder1",
        new_title="Styled Doc",
        replacements={"{{NAME}}": "Alice"},
        post_styles=[
            {"action": "set_style", "find_text": "Executive Summary", "style": "HEADING_1"},
        ],
    )

    assert result["file_id"] == "new_styled"
    assert result["replacements_made"] == {"{{NAME}}": 1}
    assert "post_styles_result" in result


@pytest.mark.asyncio
async def test_template_populate_post_styles_none(mock_services):
    """post_styles=None should not call format_document."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().copy.return_value.execute.return_value = {
        "id": "new123",
        "name": "No Styles",
        "webViewLink": "https://docs.google.com/document/d/new123/edit",
    }
    docs.documents().batchUpdate.return_value.execute.return_value = {
        "replies": [{"replaceAllText": {"occurrencesChanged": 1}}]
    }

    from gsuite_mcp.gdoc_ops import template_populate
    result = await template_populate(
        drive_service=drive,
        docs_service=docs,
        template_file_id="tmpl1",
        parent_folder_id="folder1",
        new_title="No Styles",
        replacements={"{{NAME}}": "Bob"},
    )

    assert result["file_id"] == "new123"
    assert "post_styles_result" not in result
    # documents().get() should NOT be called (format_document not invoked)
    docs.documents().get.assert_not_called()


@pytest.mark.asyncio
async def test_template_populate_post_styles_accepts_set_list(mock_services):
    """post_styles shares format_document's operation schema, list actions included."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().copy.return_value.execute.return_value = {
        "id": "new_bulleted",
        "name": "Bulleted Doc",
        "webViewLink": "https://docs.google.com/document/d/new_bulleted/edit",
    }
    docs.documents().batchUpdate.return_value.execute.return_value = {
        "replies": [{"replaceAllText": {"occurrencesChanged": 1}}]
    }
    docs.documents().get.return_value.execute.return_value = {
        "body": {"content": [
            {
                "startIndex": 1, "endIndex": 13,
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "elements": [{"startIndex": 1, "endIndex": 13,
                                  "textRun": {"content": "First item\n"}}],
                },
            },
            {
                "startIndex": 13, "endIndex": 24,
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "elements": [{"startIndex": 13, "endIndex": 24,
                                  "textRun": {"content": "Last item\n"}}],
                },
            },
        ]}
    }

    from gsuite_mcp.gdoc_ops import template_populate
    result = await template_populate(
        drive_service=drive,
        docs_service=docs,
        template_file_id="tmpl1",
        parent_folder_id="folder1",
        new_title="Bulleted Doc",
        replacements={"{{NAME}}": "Alice"},
        post_styles=[
            {"action": "set_list", "from_text": "First item", "to_text": "Last item",
             "preset": "BULLET_DISC_CIRCLE_SQUARE"},
        ],
    )

    assert result["post_styles_result"]["results"][0]["status"] == "applied"
    # Second batchUpdate (the post_styles one) carries the bullet request.
    style_requests = docs.documents().batchUpdate.call_args.kwargs["body"]["requests"]
    assert style_requests[0]["createParagraphBullets"]["range"] == {
        "startIndex": 1, "endIndex": 24,
    }
    assert style_requests[0]["createParagraphBullets"]["bulletPreset"] == (
        "BULLET_DISC_CIRCLE_SQUARE"
    )
