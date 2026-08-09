"""Tests for upload_session — in-process chunked-upload state (D9b)."""

import pytest

from gsuite_mcp import upload_session


@pytest.fixture(autouse=True)
def _clear_sessions():
    yield
    for uid in list(upload_session._SESSIONS.keys()):
        upload_session.cleanup(uid)


def test_start_session_returns_upload_id_and_creates_temp_file():
    result = upload_session.start_session(
        file_name="archive.md", mime_type="text/markdown", total_bytes=11,
    )
    upload_id = result["upload_id"]
    session = upload_session.get_session(upload_id)
    assert session is not None
    assert session["total_bytes"] == 11
    assert session["received_bytes"] == 0


def test_start_session_rejects_non_positive_total_bytes():
    with pytest.raises(ValueError):
        upload_session.start_session(
            file_name="x", mime_type="text/plain", total_bytes=0,
        )


def test_write_chunk_accumulates_bytes_in_order():
    result = upload_session.start_session(
        file_name="archive.md", mime_type="text/markdown", total_bytes=11,
    )
    upload_id = result["upload_id"]
    r1 = upload_session.write_chunk(upload_id, 0, b"hello ")
    assert r1 == {"bytes_received": 6, "total_bytes": 11}
    r2 = upload_session.write_chunk(upload_id, 1, b"world")
    assert r2 == {"bytes_received": 11, "total_bytes": 11}

    session = upload_session.get_session(upload_id)
    with open(session["temp_path"], "rb") as f:
        assert f.read() == b"hello world"


def test_write_chunk_rejects_out_of_order_index():
    result = upload_session.start_session(
        file_name="x", mime_type="text/plain", total_bytes=10,
    )
    upload_id = result["upload_id"]
    with pytest.raises(ValueError):
        upload_session.write_chunk(upload_id, 1, b"skip ahead")


def test_write_chunk_rejects_overflow_past_total_bytes():
    result = upload_session.start_session(
        file_name="x", mime_type="text/plain", total_bytes=5,
    )
    upload_id = result["upload_id"]
    with pytest.raises(ValueError):
        upload_session.write_chunk(upload_id, 0, b"way too long")


def test_write_chunk_unknown_upload_id_raises_keyerror():
    with pytest.raises(KeyError):
        upload_session.write_chunk("nonexistent", 0, b"x")


def test_finish_session_rejects_incomplete_upload():
    result = upload_session.start_session(
        file_name="x", mime_type="text/plain", total_bytes=10,
    )
    upload_id = result["upload_id"]
    upload_session.write_chunk(upload_id, 0, b"short")
    with pytest.raises(ValueError):
        upload_session.finish_session(upload_id)


def test_finish_session_verifies_sha256_when_given():
    import hashlib
    content = b"hello world"
    correct_hash = hashlib.sha256(content).hexdigest()

    result = upload_session.start_session(
        file_name="x", mime_type="text/plain", total_bytes=len(content),
        expected_sha256=correct_hash,
    )
    upload_id = result["upload_id"]
    upload_session.write_chunk(upload_id, 0, content)
    session = upload_session.finish_session(upload_id)
    assert session["received_bytes"] == len(content)


def test_finish_session_rejects_sha256_mismatch():
    result = upload_session.start_session(
        file_name="x", mime_type="text/plain", total_bytes=5,
        expected_sha256="0" * 64,
    )
    upload_id = result["upload_id"]
    upload_session.write_chunk(upload_id, 0, b"hello")
    with pytest.raises(ValueError):
        upload_session.finish_session(upload_id)


def test_finish_session_unknown_upload_id_raises_keyerror():
    with pytest.raises(KeyError):
        upload_session.finish_session("nonexistent")


def test_cleanup_removes_session_and_temp_file():
    result = upload_session.start_session(
        file_name="x", mime_type="text/plain", total_bytes=5,
    )
    upload_id = result["upload_id"]
    session = upload_session.get_session(upload_id)
    temp_path = session["temp_path"]
    import os
    assert os.path.exists(temp_path)

    upload_session.cleanup(upload_id)
    assert upload_session.get_session(upload_id) is None
    assert not os.path.exists(temp_path)
