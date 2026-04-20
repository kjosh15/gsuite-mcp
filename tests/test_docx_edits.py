import io
import zipfile

import pytest

from tests.fixtures.sample_docx import make_docx


def _extract_document_xml(docx_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
        return z.read("word/document.xml").decode("utf-8")


def test_insert_tracked_change_single_run_match():
    from gsuite_mcp.docx_edits import insert_tracked_change

    original = make_docx([("The quick brown fox", None)])
    modified = insert_tracked_change(
        original, find_text="quick", replace_text="slow", author="Claude"
    )

    xml = _extract_document_xml(modified)
    assert "<w:del " in xml
    assert "<w:delText" in xml
    assert "quick" in xml  # inside delText
    assert "<w:ins " in xml
    assert "slow" in xml
    assert 'w:author="Claude"' in xml


def test_insert_tracked_change_preserves_surrounding_text():
    from gsuite_mcp.docx_edits import insert_tracked_change

    original = make_docx([("Hello beautiful world", None)])
    modified = insert_tracked_change(
        original, "beautiful", "cruel", "Claude"
    )
    xml = _extract_document_xml(modified)
    # "Hello " and " world" must still be present in plain runs
    assert ">Hello </w:t>" in xml or "Hello" in xml
    assert "world" in xml


def test_insert_tracked_change_not_found_raises():
    from gsuite_mcp.docx_edits import insert_tracked_change, NotFoundError

    original = make_docx([("Hello world", None)])
    with pytest.raises(NotFoundError):
        insert_tracked_change(original, "xyz", "abc", "Claude")


def test_insert_tracked_change_spans_two_runs():
    from gsuite_mcp.docx_edits import insert_tracked_change

    # Three runs: "The ", "bold" (bold), " word"
    original = make_docx([
        ("The ", None),
        ("bold", {"bold": True}),
        (" word", None),
    ])
    modified = insert_tracked_change(
        original, find_text="bold word", replace_text="brave word", author="Claude"
    )
    xml = _extract_document_xml(modified)
    assert "<w:del " in xml
    assert "<w:ins " in xml
    assert "brave word" in xml
    # "The " must still be present as ordinary text (not inside del)
    assert "The " in xml


def test_insert_tracked_change_spans_three_runs():
    from gsuite_mcp.docx_edits import insert_tracked_change

    original = make_docx([
        ("The ", None),
        ("bold", {"bold": True}),
        (" word here", None),
    ])
    modified = insert_tracked_change(
        original, find_text="The bold word", replace_text="A brave word", author="Claude"
    )
    xml = _extract_document_xml(modified)
    assert "<w:del " in xml
    assert "A brave word" in xml
    assert " here" in xml  # trailing text preserved


def test_insert_tracked_change_match_at_run_boundary():
    from gsuite_mcp.docx_edits import insert_tracked_change

    original = make_docx([
        ("Hello", None),
        (" world", None),
    ])
    modified = insert_tracked_change(
        original, "Hello world", "Goodbye world", "Claude"
    )
    xml = _extract_document_xml(modified)
    assert "Goodbye world" in xml
    assert "<w:del " in xml
