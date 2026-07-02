"""Tests for docs_ops.read_document_body."""

from unittest.mock import MagicMock

import pytest

from gsuite_mcp import docs_ops, pagination


def _para(text, start):
    return {
        "startIndex": start,
        "endIndex": start + len(text),
        "paragraph": {"elements": [{"textRun": {"content": text}}]},
    }


def _doc(paras, revision="rev1"):
    content = []
    idx = 1
    for p in paras:
        content.append(_para(p, idx))
        idx += len(p)
    return {"revisionId": revision, "body": {"content": content}}


def _service(doc):
    svc = MagicMock()
    get = MagicMock()
    get.execute.return_value = doc
    svc.documents().get.return_value = get
    return svc


@pytest.mark.asyncio
async def test_reads_full_body_single_page():
    svc = _service(_doc(["Alpha\n", "Beta\n"]))
    result = await docs_ops.read_document_body(svc, "d1")
    assert result["body"] == "Alpha\nBeta\n"
    assert result["truncated"] is False
    assert result["next_cursor"] is None
    assert result["revision_id"] == "rev1"


@pytest.mark.asyncio
async def test_paginates_body_and_resumes():
    svc = _service(_doc(["A" * 60 + "\n", "B" * 60 + "\n"]))
    page1 = await docs_ops.read_document_body(svc, "d1", max_bytes=100)
    assert page1["truncated"] is True
    page2 = await docs_ops.read_document_body(svc, "d1", cursor=page1["next_cursor"], max_bytes=100)
    assert page1["body"] + page2["body"] == "A" * 60 + "\n" + "B" * 60 + "\n"
    assert page2["truncated"] is False


@pytest.mark.asyncio
async def test_stale_cursor_when_revision_changes():
    svc = _service(_doc(["Alpha\n", "Beta\n"], revision="rev2"))
    stale = pagination.encode_cursor({"kind": "doc", "offset": 1, "revision_id": "rev1"})
    result = await docs_ops.read_document_body(svc, "d1", cursor=stale)
    assert result["error"] == "STALE_CURSOR"
    assert result["revision_id"] == "rev2"


@pytest.mark.asyncio
async def test_malformed_cursor_returns_error():
    svc = _service(_doc(["Alpha\n"]))
    result = await docs_ops.read_document_body(svc, "d1", cursor="garbage!!!")
    assert result["error"] == "INVALID_CURSOR"


@pytest.mark.asyncio
async def test_invalid_offset_cursor_returns_error():
    svc = _service(_doc(["Alpha\n", "Beta\n"], revision="rev1"))
    bad_neg = pagination.encode_cursor({"kind": "doc", "offset": -1, "revision_id": "rev1"})
    bad_type = pagination.encode_cursor({"kind": "doc", "offset": "nope", "revision_id": "rev1"})
    assert (await docs_ops.read_document_body(svc, "d1", cursor=bad_neg))["error"] == "INVALID_CURSOR"
    assert (await docs_ops.read_document_body(svc, "d1", cursor=bad_type))["error"] == "INVALID_CURSOR"


# -------------------------------------------------------------------
# Tool-level: server.read_document field-projection orchestration
# -------------------------------------------------------------------


def _meta_service(mime, trashed=False):
    svc = MagicMock()
    get = MagicMock()
    get.execute.return_value = {"mimeType": mime, "trashed": trashed, "trashedTime": None}
    svc.files().get.return_value = get
    return svc


@pytest.mark.asyncio
async def test_tool_read_document_body_only(monkeypatch):
    from gsuite_mcp import server

    monkeypatch.setattr(server.auth, "get_drive_service", lambda: _meta_service("application/vnd.google-apps.document"))
    monkeypatch.setattr(server.auth, "get_docs_service", lambda: _service(_doc(["Body text\n"])))

    result = await server.read_document(file_id="d1", fields=["body"])
    assert result["body"] == "Body text\n"
    assert "comments" not in result


@pytest.mark.asyncio
async def test_tool_read_document_rejects_non_doc(monkeypatch):
    from gsuite_mcp import server

    monkeypatch.setattr(server.auth, "get_drive_service", lambda: _meta_service("application/pdf"))
    result = await server.read_document(file_id="d1")
    assert result["error"] == "NOT_A_GOOGLE_DOC"


@pytest.mark.asyncio
async def test_tool_read_document_invalid_fields(monkeypatch):
    from gsuite_mcp import server

    monkeypatch.setattr(server.auth, "get_drive_service", lambda: _meta_service("application/vnd.google-apps.document"))
    result = await server.read_document(file_id="d1", fields=["bogus"])
    assert result["error"] == "INVALID_FIELDS"
