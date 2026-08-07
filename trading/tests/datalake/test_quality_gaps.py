"""Gap test for DataQualityEngine — check_gaps detects missing bars."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from tradex_trading.datalake.quality import DataQualityEngine


@dataclass
class SimpleCandle:
    """Minimal candle for gap detection testing."""
    timestamp: datetime


def test_quality_engine_check_gaps_detects_missing_bars() -> None:
    """check_gaps detects gaps when bars are missing in a M1 series."""
    engine = DataQualityEngine()
    base = datetime(2026, 1, 1, 9, 15, tzinfo=UTC)
    # Two bars with a 5-minute gap (expected 1-minute interval)
    bars = [
        SimpleCandle(timestamp=base),
        SimpleCandle(timestamp=base + timedelta(minutes=5)),
    ]
    from tradex_domain.enums import Timeframe
    gaps = engine.check_gaps(bars, Timeframe.M1)
    assert len(gaps) >= 1
    assert gaps[0]["expected_count"] >= 3  # ~4 missing bars
