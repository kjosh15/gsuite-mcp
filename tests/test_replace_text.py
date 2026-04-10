from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def mock_services():
    with patch("gdrive_mcp.auth.get_drive_service") as mock_drive, \
         patch("gdrive_mcp.auth.get_docs_service") as mock_docs:
        drive = MagicMock()
        docs = MagicMock()
        mock_drive.return_value = drive
        mock_docs.return_value = docs
        yield {"drive": drive, "docs": docs}


@pytest.mark.asyncio
async def test_replace_text_exact_match(mock_services):
    drive = mock_services["drive"]
    docs = mock_services["docs"]
    drive.files().get.return_value.execute.return_value = {
        "name": "doc", "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }
    docs.documents().batchUpdate.return_value.execute.return_value = {
        "replies": [{"replaceAllText": {"occurrencesChanged": 3}}]
    }

    from gdrive_mcp.server import replace_text
    result = await replace_text(
        file_id="d1", find="foo", replace="bar", match_case=True, regex=False
    )

    assert result["replacements_made"] == 3
    assert result["regex_mode"] is False
    call_args = docs.documents().batchUpdate.call_args
    req = call_args.kwargs["body"]["requests"][0]
    assert req["replaceAllText"]["containsText"] == {"text": "foo", "matchCase": True}
    assert req["replaceAllText"]["replaceText"] == "bar"


@pytest.mark.asyncio
async def test_replace_text_case_insensitive(mock_services):
    drive = mock_services["drive"]
    docs = mock_services["docs"]
    drive.files().get.return_value.execute.return_value = {
        "name": "doc", "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }
    docs.documents().batchUpdate.return_value.execute.return_value = {
        "replies": [{"replaceAllText": {"occurrencesChanged": 1}}]
    }

    from gdrive_mcp.server import replace_text
    await replace_text(file_id="d1", find="Foo", replace="bar", match_case=False)

    req = docs.documents().batchUpdate.call_args.kwargs["body"]["requests"][0]
    assert req["replaceAllText"]["containsText"]["matchCase"] is False


@pytest.mark.asyncio
async def test_replace_text_not_a_google_doc_returns_error(mock_services):
    drive = mock_services["drive"]
    drive.files().get.return_value.execute.return_value = {
        "name": "file.docx",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }

    from gdrive_mcp.server import replace_text
    result = await replace_text(file_id="d1", find="x", replace="y")

    assert result["error"] == "NOT_A_GOOGLE_DOC"
    assert result["retryable"] is False
    assert "docx_suggest_edit" in result["message"]


@pytest.mark.asyncio
async def test_replace_text_zero_matches(mock_services):
    drive = mock_services["drive"]
    docs = mock_services["docs"]
    drive.files().get.return_value.execute.return_value = {
        "name": "doc", "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }
    docs.documents().batchUpdate.return_value.execute.return_value = {
        "replies": [{"replaceAllText": {}}]  # no occurrencesChanged key
    }

    from gdrive_mcp.server import replace_text
    result = await replace_text(file_id="d1", find="nothing", replace="y")
    assert result["replacements_made"] == 0


@pytest.mark.asyncio
async def test_replace_text_regex_mode(mock_services):
    drive = mock_services["drive"]
    docs = mock_services["docs"]
    drive.files().get.return_value.execute.return_value = {
        "name": "doc", "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }
    # documents.get returns body with textRuns containing plain text
    docs.documents().get.return_value.execute.return_value = {
        "body": {
            "content": [
                {
                    "startIndex": 1, "endIndex": 20,
                    "paragraph": {
                        "elements": [
                            {
                                "startIndex": 1, "endIndex": 20,
                                "textRun": {"content": "version v1.2 text\n"},
                            }
                        ]
                    },
                }
            ]
        }
    }
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}

    from gdrive_mcp.server import replace_text
    result = await replace_text(
        file_id="d1", find=r"v\d+\.\d+", replace="vNEW", regex=True
    )
    assert result["regex_mode"] is True
    assert result["replacements_made"] == 1

    req = docs.documents().batchUpdate.call_args.kwargs["body"]["requests"]
    # Should contain a delete + insert pair
    kinds = [list(r.keys())[0] for r in req]
    assert "deleteContentRange" in kinds
    assert "insertText" in kinds


@pytest.mark.asyncio
async def test_replace_text_invalid_regex_returns_error(mock_services):
    drive = mock_services["drive"]
    drive.files().get.return_value.execute.return_value = {
        "name": "doc", "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }

    from gdrive_mcp.server import replace_text
    result = await replace_text(
        file_id="d1", find="[unclosed", replace="y", regex=True
    )
    assert result["error"] == "INVALID_REGEX"
    assert result["retryable"] is False
