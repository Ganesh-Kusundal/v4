"""Incremental indicators must match their batch counterparts element-for-element.

The whole point of ``StreamingEMA``/``StreamingSMA``/``StreamingRSI`` is that a
live/replay loop can extend a series one bar at a time without recomputing
history — and get *exactly* what the batch function would have produced,
including the ``None`` warmup. If these drift, live and backtest disagree, so
the parity is asserted over randomized series (Hypothesis) and a fixed series.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st
from tradex_analytics._math import sma
from tradex_analytics.incremental import (
    StatefulIndicator,
    StreamingEMA,
    StreamingRSI,
    StreamingSMA,
)
from tradex_analytics.indicators import compute_indicator, ema, rsi
from tradex_analytics.registry import REGISTRY
from tradex_analytics.runtime import AnalyticsRuntime
from tradex_domain import OHLC, Candle, Equity, Price, Quantity, Timeframe

_PRICES = st.lists(
    st.floats(min_value=1.0, max_value=100_000.0, allow_nan=False, allow_infinity=False),
    min_size=0,
    max_size=120,
)
_PERIODS = st.integers(min_value=2, max_value=30)


def _stream(cls: type, period: int, series: list[float]) -> list[float | None]:
    indicator = cls(period)
    return [indicator.update(v) for v in series]


def _assert_parity(expected: list, got: list) -> None:
    assert len(expected) == len(got)
    for e, g in zip(expected, got):
        if e is None or g is None:
            assert e is None and g is None
        else:
            assert math.isclose(e, g, rel_tol=1e-12, abs_tol=1e-12), (e, g)


@given(series=_PRICES, period=_PERIODS)
def test_ema_matches_batch(series: list[float], period: int) -> None:
    _assert_parity(ema(series, period), _stream(StreamingEMA, period, series))


@given(series=_PRICES, period=_PERIODS)
def test_sma_matches_batch(series: list[float], period: int) -> None:
    _assert_parity(sma(series, period), _stream(StreamingSMA, period, series))


@given(series=_PRICES, period=_PERIODS)
def test_rsi_matches_batch(series: list[float], period: int) -> None:
    _assert_parity(rsi(series, period), _stream(StreamingRSI, period, series))


def test_reset_reuses_an_indicator() -> None:
    ind = StreamingEMA(3)
    first = [ind.update(v) for v in [1.0, 2.0, 3.0, 4.0, 5.0]]
    ind.reset()
    second = [ind.update(v) for v in [1.0, 2.0, 3.0, 4.0, 5.0]]
    assert first == second
    assert _assert_parity(ema([1.0, 2.0, 3.0, 4.0, 5.0], 3), second) is None


def test_value_property_tracks_the_last_emission() -> None:
    ind = StreamingSMA(2)
    assert ind.value is None
    ind.update(1.0)
    assert ind.value is None  # warmup
    ind.update(3.0)
    assert ind.value == 2.0


def test_stateful_protocol_is_satisfied() -> None:
    assert isinstance(StreamingEMA(3), StatefulIndicator)
    assert isinstance(StreamingSMA(3), StatefulIndicator)
    assert isinstance(StreamingRSI(3), StatefulIndicator)


def _candles(n: int):
    inst = Equity.of("NSE", "TEST")
    out = []
    price = 100.0
    for i in range(n):
        price += (i % 5) - 2
        p = Price(Decimal(str(round(price, 2))))
        out.append(
            Candle(
                instrument=inst,
                timeframe=Timeframe.M1,
                ohlc=OHLC(open=p, high=p, low=p, close=p),
                volume=Quantity(Decimal("1000")),
                timestamp=datetime(2026, 1, 1) + timedelta(minutes=i),
            )
        )
    return inst, out


def test_analytics_view_matches_batch_at_every_bar() -> None:
    """The shared view extends incrementally and must equal the real batch
    (``compute_indicator`` — the registry dispatch, not a private helper) at each
    step. This is the A2 guarantee across single- and multi-plot indicators.
    """
    inst, candles = _candles(60)
    runtime = AnalyticsRuntime()
    cases = [
        ("sma", {"period": 14}),
        ("ema", {"period": 14}),
        ("rsi", {"period": 14}),
        ("bollinger", {"period": 14, "num_std": 2.0}),
        ("macd", {"fast": 12, "slow": 26, "signal": 9}),
    ]
    for i, candle in enumerate(candles):
        runtime.ingest(candle)
        view = runtime.view(inst.instrument_id, Timeframe.M1)
        so_far = candles[: i + 1]
        for name, params in cases:
            got = view.indicator(name, **params)
            exp = compute_indicator(name, so_far, params, REGISTRY)
            assert set(got) == set(exp), (name, got.keys(), exp.keys())
            for plot in exp:
                assert len(got[plot]) == len(exp[plot]) == i + 1, (name, plot)
                for g, e in zip(got[plot], exp[plot]):
                    if e is None or g is None:
                        assert e is None and g is None, (name, plot, g, e)
                    else:
                        assert math.isclose(g, e, rel_tol=1e-9, abs_tol=1e-9), (
                            name, plot, g, e,
                        )
