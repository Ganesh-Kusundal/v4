"""Tests for HistoricalSeries data-export and rolling-window methods.

Covers:
- to_polars()  — lazy polars import + happy path
- to_arrow()   — lazy pyarrow import + happy path
- rolling()    — default aggregation + custom func + validation
"""

from __future__ import annotations

import builtins
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tradex_domain import (
    OHLC,
    Candle,
    CapabilityNotSupportedError,
    Equity,
    Future,
    HistoricalSeries,
    Index,
    Price,
    Quantity,
    Timeframe,
)
from tradex_domain.market import require_depth_supported


class TestRequireDepthSupported:
    """Depth is NSE-only across every broker (venue-wide constraint)."""

    def test_nse_allowed(self) -> None:
        assert require_depth_supported(Equity.of("NSE", "RELIANCE")) is None

    @pytest.mark.parametrize(
        "instrument",
        [
            Equity.of("MCX", "CRUDEOIL"),
            Equity.of("BSE", "RELIANCE"),
            Index.of("IDX", "NIFTY"),
            Future.of("NFO", "NIFTY", date(2026, 8, 25)),
        ],
    )
    def test_non_nse_raises(self, instrument) -> None:
        with pytest.raises(CapabilityNotSupportedError, match="NSE"):
            require_depth_supported(instrument)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _candle(i: int, *, base: datetime) -> Candle:
    close = Decimal(10 + i)
    return Candle(
        instrument=_eq(),
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(value=close - Decimal(1)),
            high=Price(value=close + Decimal(2)),
            low=Price(value=close - Decimal(3)),
            close=Price(value=close),
        ),
        volume=Quantity(value=Decimal(100 * (i + 1))),
        timestamp=base + timedelta(minutes=i),
    )


def _series(n: int = 5, *, base: datetime | None = None) -> HistoricalSeries:
    start = base or datetime(2026, 7, 31, 9, 0, tzinfo=UTC)
    candles = [_candle(i, base=start) for i in range(n)]
    return HistoricalSeries(
        instrument=_eq(),
        timeframe=Timeframe.M1,
        candles=candles,
        start=candles[0].timestamp,
        end=candles[-1].timestamp,
    )


def _force_missing(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make ``import <name>`` raise ImportError regardless of the environment."""
    monkeypatch.delitem(sys.modules, name, raising=False)
    real_import = builtins.__import__

    def fake_import(mod: str, *args: object, **kwargs: object) -> object:
        if mod == name:
            raise ImportError(f"No module named {name!r}")
        return real_import(mod, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


# ---------------------------------------------------------------------------
# to_polars
# ---------------------------------------------------------------------------


def test_to_polars_raises_when_polars_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_missing(monkeypatch, "polars")
    series = _series()
    with pytest.raises(CapabilityNotSupportedError, match="polars"):
        series.to_polars()


def test_to_polars_returns_dataframe_when_available() -> None:
    polars = pytest.importorskip("polars")
    series = _series()
    df = series.to_polars()
    assert isinstance(df, polars.DataFrame)
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == 5


# ---------------------------------------------------------------------------
# to_arrow
# ---------------------------------------------------------------------------


def test_to_arrow_raises_when_pyarrow_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_missing(monkeypatch, "pyarrow")
    series = _series()
    with pytest.raises(CapabilityNotSupportedError, match="pyarrow"):
        series.to_arrow()


def test_to_arrow_returns_table_when_available() -> None:
    pa = pytest.importorskip("pyarrow")
    series = _series()
    table = series.to_arrow()
    assert isinstance(table, pa.Table)
    assert table.column_names == ["timestamp", "open", "high", "low", "close", "volume"]
    assert table.num_rows == 5


# ---------------------------------------------------------------------------
# rolling
# ---------------------------------------------------------------------------


def test_rolling_invalid_window() -> None:
    series = _series()
    with pytest.raises(ValueError, match="rolling window must be positive"):
        series.rolling(0)
    with pytest.raises(ValueError, match="rolling window must be positive"):
        series.rolling(-1)


def test_rolling_default_aggregation() -> None:
    series = _series(5)
    result = series.rolling(window=3)
    assert len(result.candles) == 5
    # First candle: window clipped to [0:1] → single candle, no aggregation change
    assert result.candles[0].ohlc.open.value == series.candles[0].ohlc.open.value
    # Last candle: window covers candles [2,3,4]
    chunk = series.candles[2:5]
    from tradex_domain.market import _aggregate

    expected = _aggregate(chunk)
    assert result.candles[-1].ohlc.high.value == expected.ohlc.high.value
    assert result.candles[-1].ohlc.low.value == expected.ohlc.low.value
    assert result.candles[-1].ohlc.close.value == expected.ohlc.close.value


def test_rolling_preserves_series_metadata() -> None:
    series = _series(4)
    result = series.rolling(window=2)
    assert result.instrument == series.instrument
    assert result.timeframe == series.timeframe
    assert result.start == series.start
    assert result.end == series.end


def test_rolling_with_custom_func() -> None:
    series = _series(3)

    def _close_only(chunk: list[Candle]) -> Candle:
        """Custom aggregator: all OHLC fields set to the last close."""
        last = chunk[-1]
        return Candle(
            instrument=last.instrument,
            timeframe=last.timeframe,
            ohlc=OHLC(
                open=last.ohlc.close,
                high=last.ohlc.close,
                low=last.ohlc.close,
                close=last.ohlc.close,
            ),
            volume=last.volume,
            timestamp=last.timestamp,
        )

    result = series.rolling(window=2, func=_close_only)
    assert len(result.candles) == 3
    # Each output candle should have all OHLC == close of last bar in window
    for candle in result.candles:
        assert candle.ohlc.open.value == candle.ohlc.close.value
        assert candle.ohlc.high.value == candle.ohlc.close.value
        assert candle.ohlc.low.value == candle.ohlc.close.value
