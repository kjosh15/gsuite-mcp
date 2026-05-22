"""Tests for docs_ops helper functions (pure / no API calls)."""

from gsuite_mcp.docs_ops import _flatten_doc_text, _count_occurrences


def test_flatten_doc_text_single_paragraph():
    doc = {
        "body": {
            "content": [
                {
                    "startIndex": 1, "endIndex": 12,
                    "paragraph": {
                        "elements": [
                            {"startIndex": 1, "endIndex": 12,
                             "textRun": {"content": "hello world\n"}}
                        ]
                    },
                }
            ]
        }
    }
    flat, index_map = _flatten_doc_text(doc)
    assert flat == "hello world\n"
    assert len(index_map) == len(flat)
    assert index_map[0] == 1  # first char maps to doc index 1
    assert index_map[-1] == 12  # last char (\n) maps to doc index 12


def test_flatten_doc_text_multiple_paragraphs():
    doc = {
        "body": {
            "content": [
                {
                    "startIndex": 1, "endIndex": 7,
                    "paragraph": {
                        "elements": [
                            {"startIndex": 1, "endIndex": 7,
                             "textRun": {"content": "first\n"}}
                        ]
                    },
                },
                {
                    "startIndex": 7, "endIndex": 14,
                    "paragraph": {
                        "elements": [
                            {"startIndex": 7, "endIndex": 14,
                             "textRun": {"content": "second\n"}}
                        ]
                    },
                },
            ]
        }
    }
    flat, index_map = _flatten_doc_text(doc)
    assert flat == "first\nsecond\n"
    assert index_map[0] == 1
    assert index_map[6] == 7  # 's' of "second"


def test_flatten_doc_text_empty_doc():
    doc = {"body": {"content": []}}
    flat, index_map = _flatten_doc_text(doc)
    assert flat == ""
    assert index_map == []
