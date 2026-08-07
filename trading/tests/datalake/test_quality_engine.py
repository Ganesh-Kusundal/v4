"""Tests for DataQualityEngine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from tradex_trading.datalake.quality import DataQualityEngine


@dataclass
class SimpleCandle:
    """Simple candle for testing."""
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: datetime


@dataclass
class SimpleOHLC:
    """Simple OHLC for testing."""
    open: float
    high: float
    low: float
    close: float


class TestDataQualityEngine:
    """Tests for DataQualityEngine methods."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.engine = DataQualityEngine()

    def test_check_ohlc_integrity_detects_high_less_than_low(self) -> None:
        """Detects when high < low."""
        now = datetime.now(UTC)
        candle = SimpleCandle(
            open=100.0,
            high=90.0,  # Invalid: high < low
            low=110.0,
            close=105.0,
            volume=1000.0,
            timestamp=now,
        )
        issues = self.engine.check_ohlc_integrity([candle])
        assert len(issues) > 0
        assert any("high" in issue and "low" in issue for issue in issues)

    def test_check_ohlc_integrity_passes_valid_candles(self) -> None:
        """Passes valid candles without issues."""
        now = datetime.now(UTC)
        candle = SimpleCandle(
            open=100.0,
            high=110.0,
            low=90.0,
            close=105.0,
            volume=1000.0,
            timestamp=now,
        )
        issues = self.engine.check_ohlc_integrity([candle])
        assert len(issues) == 0

    def test_check_ohlc_integrity_detects_high_less_than_open(self) -> None:
        """Detects when high < open."""
        now = datetime.now(UTC)
        candle = SimpleCandle(
            open=120.0,
            high=110.0,  # Invalid: high < open
            low=90.0,
            close=105.0,
            volume=1000.0,
            timestamp=now,
        )
        issues = self.engine.check_ohlc_integrity([candle])
        assert any("high < open" in issue for issue in issues)

    def test_check_stale_data_detects_old_candles(self) -> None:
        """Detects candles older than max_age_seconds."""
        old_time = datetime.now(UTC) - timedelta(seconds=600)
        candle = SimpleCandle(
            open=100.0,
            high=110.0,
            low=90.0,
            close=105.0,
            volume=1000.0,
            timestamp=old_time,
        )
        issues = self.engine.check_stale_data([candle], max_age_seconds=300)
        assert len(issues) > 0
        assert any("age=" in issue and "exceeds max=" in issue for issue in issues)

    def test_check_stale_data_passes_fresh_candles(self) -> None:
        """Passes fresh candles without issues."""
        now = datetime.now(UTC)
        candle = SimpleCandle(
            open=100.0,
            high=110.0,
            low=90.0,
            close=105.0,
            volume=1000.0,
            timestamp=now,
        )
        issues = self.engine.check_stale_data([candle], max_age_seconds=300)
        assert len(issues) == 0

    def test_check_volume_detects_negative_volume(self) -> None:
        """Detects negative volume."""
        now = datetime.now(UTC)
        candle = SimpleCandle(
            open=100.0,
            high=110.0,
            low=90.0,
            close=105.0,
            volume=-500.0,  # Invalid: negative volume
            timestamp=now,
        )
        issues = self.engine.check_volume([candle])
        assert len(issues) > 0
        assert any("negative volume" in issue for issue in issues)

    def test_check_volume_passes_positive_volume(self) -> None:
        """Passes positive volume without issues."""
        now = datetime.now(UTC)
        candle = SimpleCandle(
            open=100.0,
            high=110.0,
            low=90.0,
            close=105.0,
            volume=1000.0,
            timestamp=now,
        )
        issues = self.engine.check_volume([candle])
        assert len(issues) == 0
