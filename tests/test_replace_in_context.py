# tests/test_replace_in_context.py
from unittest.mock import patch, MagicMock
import pytest

SAMPLE_DOC = {
    "body": {
        "content": [
            {
                "startIndex": 1, "endIndex": 50,
                "paragraph": {
                    "elements": [
                        {"startIndex": 1, "endIndex": 50,
                         "textRun": {"content": "In section A: replace foo with bar here.\n"}}
                    ]
                },
            },
            {
                "startIndex": 50, "endIndex": 100,
                "paragraph": {
                    "elements": [
                        {"startIndex": 50, "endIndex": 100,
                         "textRun": {"content": "In section B: keep foo unchanged here.\n"}}
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
