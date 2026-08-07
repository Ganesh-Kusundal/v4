"""Tests for HistoricalSeries.plot() method.

Covers:
- plot() returns a matplotlib Figure
- plot() on empty series raises ValueError
- plot(title=...) sets the title
- plot(show_volume=False) omits the volume subplot
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradex_domain import (
    OHLC,
    Candle,
    Equity,
    HistoricalSeries,
    Price,
    Quantity,
    Timeframe,
)

matplotlib = pytest.importorskip("matplotlib")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _candle(i: int, *, base: datetime) -> Candle:
    close = Decimal(100 + i)
    return Candle(
        instrument=_eq(),
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(value=close - Decimal(1)),
            high=Price(value=close + Decimal(2)),
            low=Price(value=close - Decimal(3)),
            close=Price(value=close),
        ),
        volume=Quantity(value=Decimal(1000 * (i + 1))),
        timestamp=base + timedelta(minutes=i),
    )


def _series(n: int = 5) -> HistoricalSeries:
    base = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)
    candles = [_candle(i, base=base) for i in range(n)]
    return HistoricalSeries(
        instrument=_eq(),
        timeframe=Timeframe.M1,
        candles=candles,
        start=candles[0].timestamp,
        end=candles[-1].timestamp,
    )


def _empty_series() -> HistoricalSeries:
    base = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)
    return HistoricalSeries(
        instrument=_eq(),
        timeframe=Timeframe.M1,
        candles=[],
        start=base,
        end=base,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_plot_returns_figure() -> None:
    """plot() should return a matplotlib Figure object."""
    import matplotlib.pyplot as plt

    series = _series()
    fig = series.plot()
    try:
        assert isinstance(fig, plt.Figure)
    finally:
        plt.close(fig)


def test_plot_empty_series_raises() -> None:
    """plot() on an empty series should raise ValueError."""
    series = _empty_series()
    with pytest.raises(ValueError, match="Cannot plot empty series"):
        series.plot()


def test_plot_sets_title() -> None:
    """plot(title='Test') should set the chart title."""
    import matplotlib.pyplot as plt

    series = _series()
    fig = series.plot(title="My Chart")
    try:
        axes = fig.get_axes()
        # The first axes is the price chart; its title should match
        assert axes[0].get_title() == "My Chart"
    finally:
        plt.close(fig)


def test_plot_show_volume_false() -> None:
    """plot(show_volume=False) should create only one subplot."""
    import matplotlib.pyplot as plt

    series = _series()
    fig = series.plot(show_volume=False)
    try:
        axes = fig.get_axes()
        # With show_volume=False, only one axes should exist
        assert len(axes) == 1
    finally:
        plt.close(fig)


def test_plot_show_volume_true_has_two_subplots() -> None:
    """plot(show_volume=True) should create two subplots (price + volume)."""
    import matplotlib.pyplot as plt

    series = _series()
    fig = series.plot(show_volume=True)
    try:
        axes = fig.get_axes()
        assert len(axes) == 2
    finally:
        plt.close(fig)
