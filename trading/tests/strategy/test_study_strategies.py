"""Study signal strategies — constructed-series signal tests.

Each strategy is fed a series with known inflection points; the emitted
signals must land exactly there. Also covers param validation and the
extensions auto-discovery protocol conformance (the suite importing
``strategies/__init__.py`` proves registration).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tradex_domain import OrderSide
from tradex_domain.enums import ExchangeId
from tradex_domain.instruments import Equity

from tradex_trading.strategy.extensions.strategies.bollinger_breakout import (
    BollingerBreakoutStrategy,
)
from tradex_trading.strategy.extensions.strategies.ema_ribbon_pullback import (
    EmaRibbonPullbackStrategy,
)
from tradex_trading.strategy.extensions.strategies.macd_cross import (
    MacdCrossStrategy,
)
from tradex_trading.strategy.extensions.strategies.rsi_reversal import (
    RsiReversalStrategy,
)
from tradex_trading.strategy.extensions.strategies.study_base import CandleShim
from tradex_trading.strategy.extensions.strategies.supertrend_flip import (
    SupertrendFlipStrategy,
)

REL = Equity.of(ExchangeId.NSE, "RELIANCE")


class _Ctx:
    def __init__(self, i: int) -> None:
        self.timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i)


def _candle(i: int, close: float) -> CandleShim:
    """Synthetic bar: open/close at close±1, high/low envelope around it."""
    o = close - 0.5
    return CandleShim(
        datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i),
        o, max(o, close) + 1.0, min(o, close) - 1.0, close, 1000.0,
    )


def _feed(strategy, closes: list[float]) -> list:
    signals = []
    for i, close in enumerate(closes):
        sig = strategy.on_bar(_Ctx(i), _candle(i, close))
        if sig is not None:
            signals.append(sig)
    return signals


# --- MACD cross -------------------------------------------------------------

def test_macd_cross_signals_at_ramp_turns():
    # long flat, sharp rally, sharp selloff -> BUY on rally, SELL on selloff
    closes = [100.0] * 45 + list(x for x in range(100, 130)) + list(x for x in range(130, 100, -2))
    strategy = MacdCrossStrategy("t", REL)
    signals = _feed(strategy, closes)
    assert [s.direction for s in signals] == [OrderSide.BUY, OrderSide.SELL]
    assert signals[0].reason == "macd_cross_up"
    assert signals[1].reason == "macd_cross_down"
    assert strategy.signals == signals


def test_macd_cross_param_validation():
    with pytest.raises(ValueError):
        MacdCrossStrategy("t", REL, fast=30, slow=20)
    with pytest.raises(ValueError):
        MacdCrossStrategy("t", REL, fast=0)


# --- RSI reversal -----------------------------------------------------------

def test_rsi_reversal_signals_on_zone_transitions():
    # flat open (RSI pinned 100) -> decline (falls through 70: SELL)
    # -> deep selloff (RSI < 30) -> recovery (rises through 30: BUY)
    closes = ([100.0] * 20
              + [100.0 - 0.8 * i for i in range(1, 25)]     # decline
              + [81.0 + 1.0 * i for i in range(1, 30)])     # recovery
    strategy = RsiReversalStrategy("t", REL)
    signals = _feed(strategy, closes)
    assert [s.direction for s in signals] == [OrderSide.SELL, OrderSide.BUY]
    assert signals[0].reason == "rsi_overbought_fade"
    assert signals[1].reason == "rsi_oversold_recovery"


def test_rsi_reversal_param_validation():
    with pytest.raises(ValueError):
        RsiReversalStrategy("t", REL, length=1)
    with pytest.raises(ValueError):
        RsiReversalStrategy("t", REL, overbought=20, oversold=50)


# --- Bollinger breakout -----------------------------------------------------

def test_bollinger_breakout_signals_on_band_breach():
    flat = [100.0] * 25
    closes = flat + [110.0, 111.0]      # well above upper band
    strategy = BollingerBreakoutStrategy("t", REL)
    signals = _feed(strategy, closes)
    assert signals[-1].direction == OrderSide.BUY
    assert signals[-1].reason == "bollinger_upper_breakout"
    closes2 = flat + [90.0, 89.0]       # well below lower band
    strategy2 = BollingerBreakoutStrategy("t2", REL)
    signals2 = _feed(strategy2, closes2)
    assert signals2[-1].direction == OrderSide.SELL
    assert signals2[-1].reason == "bollinger_lower_breakdown"


def test_bollinger_no_signal_inside_bands():
    strategy = BollingerBreakoutStrategy("t", REL)
    assert _feed(strategy, [100.0] * 30) == []


# --- Supertrend flip --------------------------------------------------------

def test_supertrend_flip_signals_on_direction_change():
    # steady rise (flip up) then steady fall (flip down)
    closes = [100.0 + 2.0 * i for i in range(30)] + [158.0 - 1.0 * i for i in range(1, 30)]
    strategy = SupertrendFlipStrategy("t", REL, period=10, multiplier=2.0)
    signals = _feed(strategy, closes)
    directions = [s.direction for s in signals]
    assert OrderSide.BUY in directions
    assert OrderSide.SELL in directions
    assert directions.index(OrderSide.BUY) < directions.index(OrderSide.SELL)


def test_supertrend_param_validation():
    with pytest.raises(ValueError):
        SupertrendFlipStrategy("t", REL, period=1)
    with pytest.raises(ValueError):
        SupertrendFlipStrategy("t", REL, multiplier=0)


# --- EMA ribbon pullback ----------------------------------------------------

def test_ribbon_pullback_bullish_touch():
    # strong uptrend into the warmup, then two bars pulled back onto the fast EMA
    closes = [100.0 + 1.0 * i for i in range(110)] + [205.0, 205.5]
    strategy = EmaRibbonPullbackStrategy("t", REL)
    signals = _feed(strategy, closes)
    assert signals
    assert signals[-1].direction == OrderSide.BUY
    assert signals[-1].reason == "ema_ribbon_pullback"


def test_ribbon_param_validation():
    with pytest.raises(ValueError):
        EmaRibbonPullbackStrategy("t", REL, lengths=(21, 9, 50, 100))
    with pytest.raises(ValueError):
        EmaRibbonPullbackStrategy("t", REL, lengths=(9, 21, 50))


# --- auto-discovery ---------------------------------------------------------

def test_new_strategies_registered_in_extensions():
    from tradex_trading.strategy.extensions.strategies import (
        bollinger_breakout_strategy,
        ema_ribbon_pullback_strategy,
        macd_cross_strategy,
        rsi_reversal_strategy,
        supertrend_flip_strategy,
    )
    for instance in (
        bollinger_breakout_strategy,
        ema_ribbon_pullback_strategy,
        macd_cross_strategy,
        rsi_reversal_strategy,
        supertrend_flip_strategy,
    ):
        assert instance.strategy_id
        assert instance.version
