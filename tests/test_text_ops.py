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
