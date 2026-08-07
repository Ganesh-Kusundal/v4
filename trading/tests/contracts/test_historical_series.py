"""HistoricalSeries contract tests — ported from v3.

Covers the time-series operations available in v4: to_dataframe (with
pandas guard), resample/_bucketize, slice, and window.

Tests for v3-only methods (to_polars, to_arrow, rolling, indicator,
indicators, stream) are intentionally omitted — those methods do not
exist in v4's HistoricalSeries.
"""

from __future__ import annotations

import builtins
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tradex_domain import (
    OHLC,
    Candle,
    CapabilityNotSupportedError,
    Equity,
    HistoricalSeries,
    Price,
    Quantity,
    Timeframe,
)


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


# ---------------------------------------------------------------------------
# Lazy-import conversions: guard branch (module missing) + stub happy path
# ---------------------------------------------------------------------------


def _force_missing(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make ``import <name>`` raise ImportError regardless of the environment."""
    monkeypatch.delitem(sys.modules, name, raising=False)
    real_import = builtins.__import__

    def fake_import(mod: str, *args: object, **kwargs: object) -> object:
        if mod == name:
            raise ImportError(f"No module named {name!r}")
        return real_import(mod, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


class _Frame:
    def __init__(self, payload: object) -> None:
        self.payload = payload


class _PandasStub:
    def DataFrame(self, rows: list[dict[str, object]]) -> _Frame:
        return _Frame(rows)


def _columns(frame: _Frame) -> set[str]:
    return set(frame.payload[0].keys()) if frame.payload else set()


def test_to_dataframe_guard_raises_without_pandas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_missing(monkeypatch, "pandas")
    with pytest.raises(CapabilityNotSupportedError):
        _series().to_dataframe()


def test_to_dataframe_happy_path_with_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pandas", _PandasStub())
    frame = _series().to_dataframe()
    assert isinstance(frame, _Frame)
    assert _columns(frame) == {"timestamp", "open", "high", "low", "close", "volume"}
    assert frame.payload[0]["close"] == Decimal(10)


# ---------------------------------------------------------------------------
# resample / _bucketize
# ---------------------------------------------------------------------------


def test_resample_m1_to_m5_aggregates() -> None:
    series = _series(5)  # 09:00..09:04 M1
    resampled = series.resample(Timeframe.M5)
    assert len(resampled.candles) == 1
    candle = resampled.candles[0]
    # open = first open, close = last close
    assert candle.ohlc.open.value == 9
    assert candle.ohlc.close.value == 14
    # high/low are Price objects per the OHLC contract (not raw Decimals)
    assert isinstance(candle.ohlc.high, Price)
    assert isinstance(candle.ohlc.low, Price)
    assert candle.ohlc.high.value == 16  # max of (12..16)
    assert candle.ohlc.low.value == 7  # min of (7..11)
    assert candle.volume.value == Decimal(100 * (1 + 2 + 3 + 4 + 5))


def test_resample_crosses_bucket_boundary() -> None:
    series = _series(6)  # 09:00..09:05 => two M5 buckets
    resampled = series.resample(Timeframe.M5)
    assert len(resampled.candles) == 2
    assert resampled.candles[0].volume.value == Decimal(100 * (1 + 2 + 3 + 4 + 5))
    assert resampled.candles[1].volume.value == Decimal(600)


def test_resample_preserves_timeframe() -> None:
    resampled = _series(5).resample(Timeframe.H1)
    assert resampled.timeframe is Timeframe.H1


# ---------------------------------------------------------------------------
# slice
# ---------------------------------------------------------------------------


def test_slice_filters_inclusive_range() -> None:
    start = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)
    series = _series(5, base=start)
    sliced = series.slice(
        start=start + timedelta(minutes=1),
        end=start + timedelta(minutes=3),
    )
    assert [c.ohlc.close.value for c in sliced.candles] == [11, 12, 13]


def test_slice_empty_when_outside_range() -> None:
    start = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)
    sliced = _series(5, base=start).slice(
        start=start + timedelta(days=1),
        end=start + timedelta(days=2),
    )
    assert sliced.candles == []


# ---------------------------------------------------------------------------
# window
# ---------------------------------------------------------------------------


def test_window_returns_trailing_candles() -> None:
    series = _series(5)
    assert [c.ohlc.close.value for c in series.window(3).candles] == [12, 13, 14]


def test_window_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        _series().window(0)
    with pytest.raises(ValueError):
        _series().window(-1)
