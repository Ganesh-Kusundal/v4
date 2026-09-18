"""Tests for ``backfill_parquet.py`` — date range parsing."""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "backfill_parquet.py"
_spec = importlib.util.spec_from_file_location("backfill_parquet", _SCRIPT)
assert _spec is not None and _spec.loader is not None, _SCRIPT
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)

_date_args = backfill._date_args


class TestDateArgs:
    """_date_args: --months fallback, --start/--end override, validation."""

    def test_months_only(self):
        """--months N (no --start/--end) returns a trailing window."""
        start, end = _date_args(3, None, None)
        assert start < end
        # Roughly 90 days back from now
        delta = (end - start).days
        assert 85 <= delta <= 95

    def test_start_and_end(self):
        """--start + --end returns the exact absolute range."""
        start, end = _date_args(3, "2026-01-01", "2026-03-15")
        assert start == datetime(2026, 1, 1)
        assert end == datetime(2026, 3, 15)

    def test_start_without_end_raises(self):
        """--start without --end raises ValueError."""
        with pytest.raises(ValueError, match="must be provided together"):
            _date_args(3, "2026-01-01", None)

    def test_end_without_start_raises(self):
        """--end without --start raises ValueError."""
        with pytest.raises(ValueError, match="must be provided together"):
            _date_args(3, None, "2026-03-15")

    def test_end_before_start_raises(self):
        """--end before --start raises ValueError."""
        with pytest.raises(ValueError, match="must be after --start"):
            _date_args(3, "2026-06-01", "2026-01-01")

    def test_end_equals_start_raises(self):
        """--end equal to --start raises ValueError (empty window)."""
        with pytest.raises(ValueError, match="must be after --start"):
            _date_args(3, "2026-01-01", "2026-01-01")
