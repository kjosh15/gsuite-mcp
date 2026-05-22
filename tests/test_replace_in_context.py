# tests/test_replace_in_context.py
from unittest.mock import patch, MagicMock
import pytest

# Build two paragraphs separated by >200 chars of padding so the
# CONTEXT_WINDOW (200) cannot reach from one paragraph's context into
# the other paragraph's "foo".
_PAD = "x" * 220  # 220-char filler to push paragraphs apart
_PARA1 = "In section A: replace foo with bar here.\n"
_PARA2 = f"In section B: {_PAD} keep foo unchanged here.\n"
_PARA2_LEN = len(_PARA2)

SAMPLE_DOC = {
    "body": {
        "content": [
            {
                "startIndex": 1, "endIndex": 1 + len(_PARA1),
                "paragraph": {
                    "elements": [
                        {"startIndex": 1, "endIndex": 1 + len(_PARA1),
                         "textRun": {"content": _PARA1}}
                    ]
                },
            },
            {
                "startIndex": 1 + len(_PARA1),
                "endIndex": 1 + len(_PARA1) + _PARA2_LEN,
                "paragraph": {
                    "elements": [
                        {"startIndex": 1 + len(_PARA1),
                         "endIndex": 1 + len(_PARA1) + _PARA2_LEN,
                         "textRun": {"content": _PARA2}}
                    ]
                },
            },
        ]
    }
}


@pytest.fixture
def mock_docs():
    docs = MagicMock()
    docs.documents().get.return_value.execute.return_value = SAMPLE_DOC
    docs.documents().batchUpdate.return_value.execute.return_value = {"replies": []}
    return docs


@pytest.mark.asyncio
async def test_replace_in_context_preceded_by(mock_docs):
    from gsuite_mcp.docs_ops import replace_in_context
    count = await replace_in_context(
        mock_docs, "f1", "foo", "qux", match_case=True,
        preceded_by="section A",
    )
    assert count == 1
    # Only one delete+insert pair
    req = mock_docs.documents().batchUpdate.call_args.kwargs["body"]["requests"]
    assert len(req) == 2


@pytest.mark.asyncio
async def test_replace_in_context_followed_by(mock_docs):
    from gsuite_mcp.docs_ops import replace_in_context
    count = await replace_in_context(
        mock_docs, "f1", "foo", "qux", match_case=True,
        followed_by="unchanged",
    )
    assert count == 1


@pytest.mark.asyncio
async def test_replace_in_context_both_anchors(mock_docs):
    from gsuite_mcp.docs_ops import replace_in_context
    count = await replace_in_context(
        mock_docs, "f1", "foo", "qux", match_case=True,
        preceded_by="section A", followed_by="bar",
    )
    assert count == 1


@pytest.mark.asyncio
async def test_replace_in_context_no_context_match(mock_docs):
    from gsuite_mcp.docs_ops import replace_in_context
    count = await replace_in_context(
        mock_docs, "f1", "foo", "qux", match_case=True,
        preceded_by="section C",
    )
    assert count == 0
    mock_docs.documents().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_replace_in_context_with_expected_count_mismatch(mock_docs):
    from gsuite_mcp.docs_ops import replace_in_context
    result = await replace_in_context(
        mock_docs, "f1", "foo", "qux", match_case=True,
        preceded_by="section A", expected_count=2,
    )
    assert isinstance(result, dict)
    assert result["error"] == "COUNT_MISMATCH"
    assert result["actual_count"] == 1
