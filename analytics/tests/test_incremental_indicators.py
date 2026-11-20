"""Incremental indicators must match their batch counterparts element-for-element.

The whole point of ``StreamingEMA``/``StreamingSMA``/``StreamingRSI`` is that a
live/replay loop can extend a series one bar at a time without recomputing
history — and get *exactly* what the batch function would have produced,
including the ``None`` warmup. If these drift, live and backtest disagree, so
the parity is asserted over randomized series (Hypothesis) and a fixed series.
"""

from __future__ import annotations

import math

from hypothesis import given
from hypothesis import strategies as st
from tradex_analytics._math import sma
from tradex_analytics.incremental import (
    StatefulIndicator,
    StreamingEMA,
    StreamingRSI,
    StreamingSMA,
)
from tradex_analytics.indicators import ema, rsi

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
