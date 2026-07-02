"""Tests for the shared pagination helpers."""

import pytest

from gsuite_mcp import pagination


def test_cursor_roundtrip():
    token = pagination.encode_cursor({"kind": "thread", "offset": 3})
    payload = pagination.decode_cursor(token)
    assert payload["kind"] == "thread"
    assert payload["offset"] == 3
    assert payload["v"] == pagination.CURSOR_VERSION


def test_decode_malformed_raises():
    with pytest.raises(ValueError):
        pagination.decode_cursor("not-base64-!!!")


def test_decode_wrong_version_raises():
    import base64
    import json
    bad = base64.urlsafe_b64encode(json.dumps({"v": 999}).encode()).decode()
    with pytest.raises(ValueError):
        pagination.decode_cursor(bad)


def test_budget_stops_before_exceeding():
    # units of 40 bytes each, budget 100 -> take 2 (80), third would be 120
    assert pagination.take_within_budget([40, 40, 40, 40], 0, 100) == 2


def test_budget_always_takes_at_least_one():
    # single unit larger than budget is still emitted (forward progress)
    assert pagination.take_within_budget([500], 0, 100) == 1


def test_budget_respects_hard_limit():
    assert pagination.take_within_budget([10, 10, 10, 10], 0, 100, hard_limit=2) == 2


def test_budget_start_past_end():
    assert pagination.take_within_budget([10, 10], 5, 100) == 5
