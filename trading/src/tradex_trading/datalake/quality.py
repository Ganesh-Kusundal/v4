"""Data quality engine — checks for gaps and duplicates."""

from __future__ import annotations

from tradex_domain import Candle, Timeframe


class DataQualityEngine:
    """Validates data quality for OHLCV bars."""

    def check_gaps(self, bars: list[Candle], timeframe: Timeframe) -> list:
        """Check for gaps in time series.

        Args:
            bars: List of candles sorted by timestamp
            timeframe: Expected timeframe

        Returns:
            List of gap descriptions (dict with start, end, expected_count)
        """
        if len(bars) < 2:
            return []

        # Sort by timestamp
        sorted_bars = sorted(bars, key=lambda c: c.timestamp)

        # Expected interval in seconds
        interval_seconds = {
            Timeframe.M1: 60,
            Timeframe.M5: 300,
            Timeframe.M15: 900,
            Timeframe.M30: 1800,
            Timeframe.H1: 3600,
            Timeframe.D1: 86400,
            Timeframe.W1: 604800,
        }[timeframe]

        gaps = []
        for i in range(len(sorted_bars) - 1):
            current = sorted_bars[i].timestamp
            next_bar = sorted_bars[i + 1].timestamp
            actual_delta = (next_bar - current).total_seconds()

            if actual_delta > interval_seconds * 1.5:  # Allow 50% tolerance
                expected_count = int(actual_delta / interval_seconds) - 1
                gaps.append({
                    'start': current,
                    'end': next_bar,
                    'expected_count': expected_count,
                })

        return gaps

    def check_duplicates(self, bars: list[Candle]) -> list:
        """Check for duplicate timestamps.

        Args:
            bars: List of candles

        Returns:
            List of duplicate timestamps
        """
        if not bars:
            return []

        timestamps = [c.timestamp for c in bars]
        seen = set()
        duplicates = []

        for ts in timestamps:
            if ts in seen:
                duplicates.append(ts)
            seen.add(ts)

        return duplicates

    def check_ohlc_integrity(self, candles: list) -> list[str]:
        """Validate OHLCV integrity: high >= low, high >= open/close, low <= open/close.

        Args:
            candles: List of candle objects with open/high/low/close attributes

        Returns:
            List of issue descriptions
        """
        issues = []
        for i, c in enumerate(candles):
            if hasattr(c, 'high') and hasattr(c, 'low'):
                if c.high < c.low:
                    issues.append(f"candle[{i}]: high ({c.high}) < low ({c.low})")
                if hasattr(c, 'open') and c.high < c.open:
                    issues.append(f"candle[{i}]: high < open")
                if hasattr(c, 'close') and c.high < c.close:
                    issues.append(f"candle[{i}]: high < close")
                if hasattr(c, 'open') and c.low > c.open:
                    issues.append(f"candle[{i}]: low > open")
                if hasattr(c, 'close') and c.low > c.close:
                    issues.append(f"candle[{i}]: low > close")
        return issues

    def check_stale_data(self, candles: list, max_age_seconds: float = 300) -> list[str]:
        """Detect bars older than expected.

        Args:
            candles: List of candle objects with timestamp attribute
            max_age_seconds: Maximum acceptable age in seconds

        Returns:
            List of issue descriptions
        """
        from datetime import UTC, datetime
        issues = []
        now = datetime.now(UTC)
        for i, c in enumerate(candles):
            ts = getattr(c, 'timestamp', None)
            if ts is not None:
                age = (now - ts).total_seconds()
                if age > max_age_seconds:
                    issues.append(f"candle[{i}]: age={age:.0f}s exceeds max={max_age_seconds}s")
        return issues

    def check_volume(self, candles: list) -> list[str]:
        """Detect negative volume.

        Args:
            candles: List of candle objects with volume attribute

        Returns:
            List of issue descriptions
        """
        issues = []
        for i, c in enumerate(candles):
            vol = getattr(c, 'volume', None)
            if vol is not None:
                val = vol.value if hasattr(vol, 'value') else vol
                if val < 0:
                    issues.append(f"candle[{i}]: negative volume ({val})")
        return issues


__all__ = ["DataQualityEngine"]
