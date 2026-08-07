"""Tests for indicator warm-up support."""

from __future__ import annotations

from tradex_trading.analytics.warmup import WarmupFilter, warmup_indicator


class MockIndicator:
    """Simple mock indicator that returns the input value."""

    def __init__(self) -> None:
        self.values: list[float] = []

    def update(self, value: float) -> float:
        self.values.append(value)
        return value


class TestWarmupFilter:
    """WarmupFilter suppresses output during warm-up period."""

    def test_returns_none_during_warmup(self) -> None:
        indicator = MockIndicator()
        filter = WarmupFilter(indicator, period=3)

        # First 3 updates should return None (warm-up period)
        assert filter.update(1.0) is None
        assert filter.update(2.0) is None
        assert filter.update(3.0) is None

    def test_returns_values_after_warmup(self) -> None:
        indicator = MockIndicator()
        filter = WarmupFilter(indicator, period=3)

        # Consume warm-up period
        filter.update(1.0)
        filter.update(2.0)
        filter.update(3.0)

        # After warm-up, values should be returned
        assert filter.update(4.0) == 4.0
        assert filter.update(5.0) == 5.0

    def test_warmed_up_property(self) -> None:
        indicator = MockIndicator()
        filter = WarmupFilter(indicator, period=2)

        assert not filter.warmed_up
        filter.update(1.0)
        assert not filter.warmed_up
        filter.update(2.0)
        assert not filter.warmed_up  # Still in warm-up (count == period)
        filter.update(3.0)
        assert filter.warmed_up  # Now warmed up (count > period)

    def test_count_property(self) -> None:
        indicator = MockIndicator()
        filter = WarmupFilter(indicator, period=5)

        assert filter.count == 0
        filter.update(1.0)
        assert filter.count == 1
        filter.update(2.0)
        assert filter.count == 2


class TestWarmupIndicatorHelper:
    """warmup_indicator helper creates WarmupFilter."""

    def test_creates_warmup_filter(self) -> None:
        indicator = MockIndicator()
        filter = warmup_indicator(indicator, period=5)

        assert isinstance(filter, WarmupFilter)
        # Verify it behaves correctly
        for _ in range(5):
            assert filter.update(1.0) is None
        assert filter.update(2.0) == 2.0
