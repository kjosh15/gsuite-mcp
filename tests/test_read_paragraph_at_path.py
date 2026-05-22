# tests/test_read_paragraph_at_path.py
from unittest.mock import patch, MagicMock

import pytest

from gsuite_mcp.docs_ops import _build_doc_tree, _resolve_path


def _make_heading(idx, start, end, text, style="HEADING_1"):
    return {
        "startIndex": start, "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": style},
            "elements": [
                {"startIndex": start, "endIndex": end,
                 "textRun": {"content": text + "\n"}}
            ],
        },
    }


def _make_bullet(idx, start, end, text, nesting_level=0):
    return {
        "startIndex": start, "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "bullet": {"nestingLevel": nesting_level},
            "elements": [
                {"startIndex": start, "endIndex": end,
                 "textRun": {"content": text + "\n"}}
            ],
        },
    }


SAMPLE_CONTENT = [
    {"startIndex": 0, "endIndex": 1},  # structural element (no paragraph)
    _make_heading(1, 1, 7, "TASKS", "HEADING_1"),
    _make_bullet(2, 7, 15, "Career", 0),
    _make_bullet(3, 15, 50, "Careers that allow us to live in Iceland", 1),
    _make_bullet(4, 50, 70, "Teaching English", 2),
    _make_bullet(5, 70, 95, "Software engineering", 2),
    _make_bullet(6, 95, 110, "Finance", 1),
    _make_heading(7, 110, 120, "NOTES", "HEADING_1"),
    _make_bullet(8, 120, 135, "Random note", 0),
]


def test_build_doc_tree():
    tree = _build_doc_tree(SAMPLE_CONTENT)
    # Top level should have 2 heading nodes: TASKS, NOTES
    assert len(tree) == 2
    assert tree[0]["text"] == "TASKS"
    assert tree[1]["text"] == "NOTES"


def test_resolve_path_heading():
    tree = _build_doc_tree(SAMPLE_CONTENT)
    node = _resolve_path(tree, ["TASKS"])
    assert node is not None
    assert node["text"] == "TASKS"


def test_resolve_path_nested_bullet():
    tree = _build_doc_tree(SAMPLE_CONTENT)
    node = _resolve_path(tree, ["TASKS", "Career", "Careers that allow"])
    assert node is not None
    assert "Careers that allow" in node["text"]


def test_resolve_path_positional():
    tree = _build_doc_tree(SAMPLE_CONTENT)
    node = _resolve_path(tree, ["TASKS", "Career", "#2"])
    assert node is not None
    assert node["text"].startswith("Finance")


def test_resolve_path_not_found():
    tree = _build_doc_tree(SAMPLE_CONTENT)
    node = _resolve_path(tree, ["TASKS", "Nonexistent"])
    assert node is None


def test_resolve_path_include_children():
    tree = _build_doc_tree(SAMPLE_CONTENT)
    node = _resolve_path(tree, ["TASKS", "Career", "Careers that allow"])
    assert node is not None
    assert len(node.get("children", [])) == 2
    assert node["children"][0]["text"] == "Teaching English"
    assert node["children"][1]["text"] == "Software engineering"


# ---------------------------------------------------------------------------
# Integration tests — read_paragraph_at_path MCP tool
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_services():
    with patch("gsuite_mcp.auth.get_drive_service") as mock_drive, \
         patch("gsuite_mcp.auth.get_docs_service") as mock_docs:
        drive = MagicMock()
        docs = MagicMock()
        mock_drive.return_value = drive
        mock_docs.return_value = docs
        drive.files().get.return_value.execute.return_value = {
            "name": "doc", "mimeType": "application/vnd.google-apps.document",
        }
        docs.documents().get.return_value.execute.return_value = {
            "body": {"content": SAMPLE_CONTENT}
        }
        yield {"drive": drive, "docs": docs}


@pytest.mark.asyncio
async def test_read_paragraph_at_path_tool(mock_services):
    from gsuite_mcp.server import read_paragraph_at_path
    result = await read_paragraph_at_path(
        file_id="d1", path="TASKS / Career / Careers that allow"
    )
    assert "Careers that allow" in result["text"]
    assert result["nesting_level"] == 1
    assert "children" not in result


@pytest.mark.asyncio
async def test_read_paragraph_at_path_with_children(mock_services):
    from gsuite_mcp.server import read_paragraph_at_path
    result = await read_paragraph_at_path(
        file_id="d1", path="TASKS / Career / Careers that allow",
        include_children=True,
    )
    assert len(result["children"]) == 2


@pytest.mark.asyncio
async def test_read_paragraph_at_path_not_found(mock_services):
    from gsuite_mcp.server import read_paragraph_at_path
    result = await read_paragraph_at_path(
        file_id="d1", path="TASKS / Nonexistent"
    )
    assert result["error"] == "PATH_NOT_FOUND"


@pytest.mark.asyncio
async def test_read_paragraph_at_path_not_a_doc(mock_services):
    mock_services["drive"].files().get.return_value.execute.return_value = {
        "name": "file.txt", "mimeType": "text/plain",
    }
    from gsuite_mcp.server import read_paragraph_at_path
    result = await read_paragraph_at_path(file_id="d1", path="anything")
    assert result["error"] == "NOT_A_GOOGLE_DOC"
