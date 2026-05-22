# tests/test_read_paragraph_at_path.py
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
