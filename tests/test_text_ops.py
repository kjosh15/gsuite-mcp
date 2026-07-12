"""Tests for text_ops pure functions."""

import pytest

from gsuite_mcp import text_ops


class TestIsSupportedMime:
    def test_text_plain_allowed(self):
        assert text_ops.is_supported_mime("text/plain") is True

    def test_text_markdown_allowed(self):
        assert text_ops.is_supported_mime("text/markdown") is True

    def test_application_json_allowed(self):
        assert text_ops.is_supported_mime("application/json") is True

    def test_application_x_yaml_allowed(self):
        assert text_ops.is_supported_mime("application/x-yaml") is True

    def test_google_doc_mime_refused(self):
        assert text_ops.is_supported_mime("application/vnd.google-apps.document") is False

    def test_binary_mime_refused(self):
        assert text_ops.is_supported_mime("application/octet-stream") is False

    def test_pdf_refused(self):
        assert text_ops.is_supported_mime("application/pdf") is False


class TestIsGoogleAppsMime:
    def test_google_doc(self):
        assert text_ops.is_google_apps_mime("application/vnd.google-apps.document") is True

    def test_plain_text(self):
        assert text_ops.is_google_apps_mime("text/plain") is False


class TestDetectLineEnding:
    def test_crlf_detected(self):
        assert text_ops.detect_line_ending("line1\r\nline2\r\n") == "\r\n"

    def test_lf_detected(self):
        assert text_ops.detect_line_ending("line1\nline2\n") == "\n"

    def test_no_newlines_defaults_lf(self):
        assert text_ops.detect_line_ending("no newlines here") == "\n"


class TestDecodeEncodeText:
    def test_decode_valid_utf8(self):
        result = text_ops.decode_text("hello world".encode("utf-8"))
        assert result["text"] == "hello world"
        assert result["line_ending"] == "\n"

    def test_decode_invalid_utf8_raises(self):
        with pytest.raises(UnicodeDecodeError):
            text_ops.decode_text(b"\xff\xfe\x00\x01invalid")

    def test_decode_normalizes_crlf_to_lf(self):
        result = text_ops.decode_text(b"line1\r\nline2\r\n")
        assert result["text"] == "line1\nline2\n"
        assert result["line_ending"] == "\r\n"

    def test_encode_restores_crlf(self):
        raw = text_ops.encode_text("line1\nline2\n", "\r\n")
        assert raw == b"line1\r\nline2\r\n"

    def test_encode_lf_passthrough(self):
        raw = text_ops.encode_text("line1\nline2\n", "\n")
        assert raw == b"line1\nline2\n"

    def test_roundtrip_preserves_crlf(self):
        original = "Status: draft\r\nOwner: josh\r\n"
        decoded = text_ops.decode_text(original.encode("utf-8"))
        re_encoded = text_ops.encode_text(decoded["text"], decoded["line_ending"])
        assert re_encoded.decode("utf-8") == original


class TestCountAndApplyReplace:
    def test_count_matches_literal(self):
        assert text_ops.count_matches("abc abc abc", "abc") == 3

    def test_count_matches_case_insensitive(self):
        assert text_ops.count_matches("ABC abc AbC", "abc", match_case=False) == 3

    def test_count_matches_regex(self):
        assert text_ops.count_matches("v1.0 v2.0 v3.0", r"v\d\.0", regex=True) == 3

    def test_apply_replace_literal(self):
        assert text_ops.apply_replace("hello world", "world", "there") == "hello there"

    def test_apply_replace_case_insensitive(self):
        result = text_ops.apply_replace("Status: OLD", "old", "NEW", match_case=False)
        assert result == "Status: NEW"

    def test_apply_replace_regex(self):
        result = text_ops.apply_replace("v1.0", r"v(\d)\.0", r"v\1.1", regex=True)
        assert result == "v1.1"

    def test_apply_replace_multiline_pattern(self):
        content = "## Status\nold line\n## Next"
        result = text_ops.apply_replace(content, "## Status\nold line", "## Status\nnew line")
        assert result == "## Status\nnew line\n## Next"

    def test_apply_replace_no_match_returns_unchanged(self):
        assert text_ops.apply_replace("hello", "xyz", "abc") == "hello"

    def test_count_matches_no_overlap_on_self_overlapping_pattern(self):
        assert text_ops.count_matches("aaaa", "aa") == 2

    def test_apply_replace_no_overlap_on_self_overlapping_pattern(self):
        assert text_ops.apply_replace("aaaa", "aa", "b") == "bb"


class TestApplyBatch:
    def test_single_edit_applies(self):
        result = text_ops.apply_batch("hello world", [
            {"find": "world", "replace": "there", "expected_count": 1},
        ])
        assert result["content"] == "hello there"
        assert result["aborted_at"] is None
        assert result["per_edit"] == [
            {"index": 0, "find_preview": "world", "matches_found": 1, "applied": True},
        ]
        assert result["chars_deleted"] == 5
        assert result["chars_inserted"] == 5

    def test_sequential_edits_see_prior_result(self):
        """Edit 2 must operate on the output of edit 1, not the original."""
        result = text_ops.apply_batch("A B", [
            {"find": "A", "replace": "A-X", "expected_count": 1},
            {"find": "A-X", "replace": "A-X-Y", "expected_count": 1},
        ])
        assert result["content"] == "A-X-Y B"
        assert result["aborted_at"] is None

    def test_count_mismatch_aborts_before_any_write(self):
        result = text_ops.apply_batch("foo foo", [
            {"find": "foo", "replace": "bar", "expected_count": 1},
        ])
        assert result["content"] is None
        assert result["aborted_at"] == 0
        assert result["per_edit"][0]["matches_found"] == 2
        assert result["per_edit"][0]["applied"] is False

    def test_batch_aborts_at_failing_edit_leaves_content_none(self):
        """7 edits, edit index 3 fails — no edit (including 0-2) is reflected in content."""
        edits = [{"find": f"marker{i}", "replace": f"replaced{i}", "expected_count": 1} for i in range(7)]
        content = "marker0 marker1 marker2 marker3 marker3 marker4 marker5 marker6"
        result = text_ops.apply_batch(content, edits)
        assert result["content"] is None
        assert result["aborted_at"] == 3
        assert result["per_edit"][3]["matches_found"] == 2
        assert len(result["per_edit"]) == 4  # edits 0,1,2 succeeded, 3 failed, 4-6 never attempted

    def test_no_expected_count_never_aborts(self):
        result = text_ops.apply_batch("foo foo foo", [
            {"find": "foo", "replace": "bar"},
        ])
        assert result["content"] == "bar bar bar"
        assert result["aborted_at"] is None
