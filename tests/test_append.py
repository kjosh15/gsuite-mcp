from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def mock_services():
    """Mock drive, docs, and sheets services."""
    with patch("gsuite_mcp.auth.get_drive_service") as mock_drive, \
         patch("gsuite_mcp.auth.get_docs_service") as mock_docs, \
         patch("gsuite_mcp.auth.get_sheets_service") as mock_sheets:
        drive = MagicMock()
        docs = MagicMock()
        sheets = MagicMock()
        mock_drive.return_value = drive
        mock_docs.return_value = docs
        mock_sheets.return_value = sheets
        yield {"drive": drive, "docs": docs, "sheets": sheets}


@pytest.mark.asyncio
async def test_append_to_google_doc_uses_docs_api(mock_services):
    """Appending to a Google Doc uses Docs API batchUpdate with InsertTextRequest."""
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    # files.get returns a Google Doc
    drive.files().get.return_value.execute.return_value = {
        "name": "Index",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }
    # documents.get returns a doc body with endIndex
    docs.documents().get.return_value.execute.return_value = {
        "body": {
            "content": [
                {"endIndex": 1},
                {"endIndex": 42},
            ]
        }
    }
    docs.documents().batchUpdate.return_value.execute.return_value = {}

    from gsuite_mcp.server import append_to_file
    result = await append_to_file(
        file_id="doc123", content="new line", separator="\n"
    )

    assert result["mode"] == "docs_native"
    assert result["file_id"] == "doc123"
    assert result["bytes_appended"] > 0

    # verify batchUpdate was called with InsertTextRequest
    call_args = docs.documents().batchUpdate.call_args
    requests = call_args.kwargs["body"]["requests"]
    assert len(requests) == 1
    assert "insertText" in requests[0]
    assert requests[0]["insertText"]["text"] == "\nnew line"


@pytest.mark.asyncio
async def test_append_to_google_sheet_uses_sheets_api(mock_services):
    """Appending to a Google Sheet uses Sheets API values.append."""
    drive = mock_services["drive"]
    sheets = mock_services["sheets"]

    drive.files().get.return_value.execute.return_value = {
        "name": "Pipeline",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }
    # spreadsheets.get returns sheet metadata so we can find the first sheet title
    sheets.spreadsheets().get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Sheet1"}}],
    }
    sheets.spreadsheets().values().append.return_value.execute.return_value = {
        "updates": {"updatedRange": "Sheet1!A42:C42"}
    }

    from gsuite_mcp.server import append_to_file
    result = await append_to_file(
        file_id="sheet123",
        content="col1,col2,col3\nrow2c1,row2c2,row2c3",
        separator="",
    )

    assert result["mode"] == "sheets_native"
    assert result["bytes_appended"] > 0

    # verify values.append was called with parsed rows
    call_args = sheets.spreadsheets().values().append.call_args
    assert call_args.kwargs["range"] == "Sheet1"
    assert call_args.kwargs["valueInputOption"] == "USER_ENTERED"
    body = call_args.kwargs["body"]
    assert body["values"] == [
        ["col1", "col2", "col3"],
        ["row2c1", "row2c2", "row2c3"],
    ]


@pytest.mark.asyncio
async def test_append_to_plain_file_roundtrips(mock_services):
    """Appending to a plain file downloads, concats, and re-uploads."""
    drive = mock_services["drive"]

    drive.files().get.return_value.execute.return_value = {
        "name": "notes.md",
        "mimeType": "text/markdown",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }
    drive.files().get_media.return_value.execute.return_value = b"existing content"
    drive.files().update.return_value.execute.return_value = {
        "id": "plain1",
        "name": "notes.md",
        "webViewLink": "https://example.com",
        "version": "2",
        "modifiedTime": "2026-04-10T12:05:00Z",
    }

    from gsuite_mcp.server import append_to_file
    result = await append_to_file(
        file_id="plain1", content="new line", separator="\n"
    )

    assert result["mode"] == "plain_roundtrip"
    assert result["bytes_appended"] == len(b"\nnew line")
    # verify update was called with concatenated content
    drive.files().update.assert_called_once()


@pytest.mark.asyncio
async def test_append_to_google_doc_returns_revision_ids(mock_services):
    drive = mock_services["drive"]
    docs = mock_services["docs"]

    drive.files().get.return_value.execute.return_value = {
        "name": "Index", "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }
    docs.documents().get.return_value.execute.return_value = {
        "body": {"content": [{"endIndex": 1}, {"endIndex": 42}]}
    }
    docs.documents().batchUpdate.return_value.execute.return_value = {}

    call_count = {"n": 0}
    def revisions_execute():
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"revisions": [{"id": "rev_before"}]}
        return {"revisions": [{"id": "rev_before"}, {"id": "rev_after"}]}
    revisions_mock = MagicMock()
    revisions_mock.execute = revisions_execute
    drive.revisions().list.return_value = revisions_mock

    from gsuite_mcp.server import append_to_file
    result = await append_to_file(file_id="doc123", content="new line", separator="\n")

    assert result["revision_id_before"] == "rev_before"
    assert result["revision_id_after"] == "rev_after"


@pytest.mark.asyncio
async def test_append_to_google_sheet_returns_revision_ids(mock_services):
    drive = mock_services["drive"]
    sheets = mock_services["sheets"]

    drive.files().get.return_value.execute.return_value = {
        "name": "Pipeline", "mimeType": "application/vnd.google-apps.spreadsheet",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }
    sheets.spreadsheets().get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Sheet1"}}],
    }
    sheets.spreadsheets().values().append.return_value.execute.return_value = {
        "updates": {"updatedRange": "Sheet1!A42:C42"}
    }

    call_count = {"n": 0}
    def revisions_execute():
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"revisions": [{"id": "rev1"}]}
        return {"revisions": [{"id": "rev1"}, {"id": "rev2"}]}
    revisions_mock = MagicMock()
    revisions_mock.execute = revisions_execute
    drive.revisions().list.return_value = revisions_mock

    from gsuite_mcp.server import append_to_file
    result = await append_to_file(file_id="sheet123", content="a,b", separator="")

    assert result["revision_id_before"] == "rev1"
    assert result["revision_id_after"] == "rev2"


@pytest.mark.asyncio
async def test_append_to_plain_file_has_no_revision_ids(mock_services):
    drive = mock_services["drive"]

    drive.files().get.return_value.execute.return_value = {
        "name": "notes.md", "mimeType": "text/markdown",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }
    drive.files().get_media.return_value.execute.return_value = b"existing content"
    drive.files().update.return_value.execute.return_value = {
        "id": "plain1", "name": "notes.md", "webViewLink": "https://example.com",
        "version": "2", "modifiedTime": "2026-04-10T12:05:00Z",
    }

    from gsuite_mcp.server import append_to_file
    result = await append_to_file(file_id="plain1", content="new line", separator="\n")

    assert "revision_id_before" not in result
    assert "revision_id_after" not in result
    drive.revisions().list.assert_not_called()


@pytest.mark.asyncio
async def test_append_to_google_doc_post_write_revision_lookup_failure():
    """revisions().list() raising on the POST-write call must not turn a
    successful append into a reported error; revision_id_after falls back
    to None instead."""
    with patch("gsuite_mcp.auth.get_drive_service") as mock_drive, \
         patch("gsuite_mcp.auth.get_docs_service") as mock_docs:
        drive = MagicMock()
        docs = MagicMock()
        mock_drive.return_value = drive
        mock_docs.return_value = docs

        drive.files().get.return_value.execute.return_value = {
            "name": "Index", "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "2026-04-10T12:00:00Z",
        }
        docs.documents().get.return_value.execute.return_value = {
            "body": {"content": [{"endIndex": 1}, {"endIndex": 42}]}
        }
        docs.documents().batchUpdate.return_value.execute.return_value = {}

        call_count = {"n": 0}
        def revisions_execute():
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"revisions": [{"id": "rev_before"}]}
            raise Exception("transient API error")
        revisions_mock = MagicMock()
        revisions_mock.execute = revisions_execute
        drive.revisions().list.return_value = revisions_mock

        from gsuite_mcp.server import append_to_file
        result = await append_to_file(file_id="doc123", content="new line", separator="\n")

        assert "error" not in result
        assert result["mode"] == "docs_native"
        assert result["revision_id_before"] == "rev_before"
        assert result["revision_id_after"] is None
        docs.documents().batchUpdate.assert_called_once()


@pytest.mark.asyncio
async def test_append_to_google_doc_pre_write_revision_lookup_failure():
    """revisions().list() raising on the PRE-write call must not abort the
    append; revision_id_before falls back to None and the write still
    happens, with revision_id_after computed normally from the next call."""
    with patch("gsuite_mcp.auth.get_drive_service") as mock_drive, \
         patch("gsuite_mcp.auth.get_docs_service") as mock_docs:
        drive = MagicMock()
        docs = MagicMock()
        mock_drive.return_value = drive
        mock_docs.return_value = docs

        drive.files().get.return_value.execute.return_value = {
            "name": "Index", "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "2026-04-10T12:00:00Z",
        }
        docs.documents().get.return_value.execute.return_value = {
            "body": {"content": [{"endIndex": 1}, {"endIndex": 42}]}
        }
        docs.documents().batchUpdate.return_value.execute.return_value = {}

        call_count = {"n": 0}
        def revisions_execute():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise Exception("transient API error")
            return {"revisions": [{"id": "rev_after"}]}
        revisions_mock = MagicMock()
        revisions_mock.execute = revisions_execute
        drive.revisions().list.return_value = revisions_mock

        from gsuite_mcp.server import append_to_file
        result = await append_to_file(file_id="doc123", content="new line", separator="\n")

        assert "error" not in result
        assert result["mode"] == "docs_native"
        assert result["revision_id_before"] is None
        assert result["revision_id_after"] == "rev_after"
        docs.documents().batchUpdate.assert_called_once()


@pytest.mark.asyncio
async def test_append_to_file_rejects_expected_bytes_mismatch(mock_services):
    from gsuite_mcp.server import append_to_file
    result = await append_to_file(
        file_id="doc123", content="new line", expected_bytes=999,
    )
    assert result["error"] == "PAYLOAD_SIZE_MISMATCH"
    mock_services["drive"].files().get.assert_not_called()


@pytest.mark.asyncio
async def test_append_to_file_rejects_expected_sha256_mismatch(mock_services):
    from gsuite_mcp.server import append_to_file
    result = await append_to_file(
        file_id="doc123", content="new line", expected_sha256="0" * 64,
    )
    assert result["error"] == "PAYLOAD_HASH_MISMATCH"
    mock_services["drive"].files().get.assert_not_called()


@pytest.mark.asyncio
async def test_append_to_file_accepts_matching_expected_bytes(mock_services):
    import hashlib
    drive = mock_services["drive"]
    docs = mock_services["docs"]
    drive.files().get.return_value.execute.return_value = {
        "name": "Index", "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-04-10T12:00:00Z",
    }
    docs.documents().get.return_value.execute.return_value = {
        "body": {"content": [{"endIndex": 1}, {"endIndex": 42}]}
    }
    docs.documents().batchUpdate.return_value.execute.return_value = {}
    drive.revisions().list.return_value.execute.return_value = {"revisions": []}

    content = "new line"
    expected_bytes = len(("\n" + content).encode("utf-8"))
    expected_sha256 = hashlib.sha256(("\n" + content).encode("utf-8")).hexdigest()

    from gsuite_mcp.server import append_to_file
    result = await append_to_file(
        file_id="doc123", content=content,
        expected_bytes=expected_bytes, expected_sha256=expected_sha256,
    )
    assert "error" not in result
