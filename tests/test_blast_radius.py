# tests/test_blast_radius.py
"""Tests for the blast-radius guard helper."""

import pytest
from gsuite_mcp.docs_ops import check_blast_radius


class TestCheckBlastRadius:
    def test_small_edit_passes(self):
        """Small delta, small ratio — no guard trip."""
        result = check_blast_radius(chars_deleted=50, chars_inserted=30)
        assert result is None

    def test_large_delta_and_ratio_trips(self):
        """Deleted - inserted > 200 AND deleted > 2x inserted — trips guard."""
        result = check_blast_radius(chars_deleted=6653, chars_inserted=201)
        assert result is not None
        assert result["error"] == "BLAST_RADIUS_EXCEEDED"
        assert result["chars_deleted"] == 6653
        assert result["chars_inserted"] == 201
        assert result["net_change"] == 201 - 6653
        assert "confirm_delete_chars=6653" in result["message"]
        assert result["retryable"] is True

    def test_large_delta_but_small_ratio_passes(self):
        """Deleted - inserted > 200, but deleted < 2x inserted — passes."""
        result = check_blast_radius(chars_deleted=500, chars_inserted=400)
        assert result is None

    def test_large_ratio_but_small_delta_passes(self):
        """Deleted > 2x inserted, but delta < 200 — passes."""
        result = check_blast_radius(chars_deleted=100, chars_inserted=10)
        assert result is None

    def test_pure_deletion_trips(self):
        """Deleting 300 chars and inserting 0 always trips."""
        result = check_blast_radius(chars_deleted=300, chars_inserted=0)
        assert result is not None
        assert result["error"] == "BLAST_RADIUS_EXCEEDED"

    def test_confirm_bypasses_guard(self):
        """Passing correct confirm_delete_chars bypasses the guard."""
        result = check_blast_radius(
            chars_deleted=6653, chars_inserted=201,
            confirm_delete_chars=6653,
        )
        assert result is None

    def test_confirm_wrong_value_still_trips(self):
        """Wrong confirm_delete_chars value does not bypass."""
        result = check_blast_radius(
            chars_deleted=6653, chars_inserted=201,
            confirm_delete_chars=999,
        )
        assert result is not None
        assert result["error"] == "BLAST_RADIUS_EXCEEDED"

    def test_custom_thresholds(self):
        """Custom min_delta and max_ratio override defaults."""
        # Would trip default (delta=250>200, ratio=5>2) but not custom
        result = check_blast_radius(
            chars_deleted=300, chars_inserted=50,
            min_delta=500, max_ratio=10,
        )
        assert result is None

    def test_equal_delete_insert_passes(self):
        """Same chars deleted and inserted — no guard trip."""
        result = check_blast_radius(chars_deleted=5000, chars_inserted=5000)
        assert result is None
