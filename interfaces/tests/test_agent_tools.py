"""Agent tools: JSON-safe wrappers over the deterministic backtest core."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.market import OHLC, Candle
from tradex_domain.strategy import Signal
from tradex_domain.value_objects import Price, Quantity
from tradex_interfaces.agent.tools import (
    backtest,
    bars_to_records,
    list_strategies,
    run_backtest,
)

_INST = Equity.of("NSE", "TEST")
_T0 = datetime(2026, 1, 1, 9, 15, tzinfo=UTC)


def _candle(i: int, o: float) -> Candle:
    p = Price(Decimal(str(o)))
    return Candle(
        instrument=_INST,
        timeframe=Timeframe.M1,
        ohlc=OHLC(open=p, high=p, low=p, close=p),
        volume=Quantity(Decimal("1000")),
        timestamp=_T0 + timedelta(minutes=i),
    )


class _BuyAndHold:
    strategy_id = "hold"
    version = "1.0.0"

    def __init__(self) -> None:
        self.signals: list[Signal] = []
        self._done = False

    @property
    def instruments(self):
        return [_INST.instrument_id]

    def on_start(self, context) -> None:
        return None

    def on_stop(self, context) -> None:
        return None

    def on_quote(self, context, quote):
        return None

    def on_depth(self, context, depth):
        return None

    def on_fill(self, context, fill) -> None:
        return None

    def on_event(self, event) -> None:
        return None

    def on_bar(self, context, candle):
        if self._done:
            return None
        self._done = True
        self.signals.append(
            Signal(
                instrument=_INST, direction=OrderSide.BUY, strength=1.0,
                reason="entry", quantity=Quantity(Decimal("10")),
                metadata={}, timestamp=candle.timestamp,
            )
        )
        return None


def _run() -> dict:
    candles = [_candle(i, p) for i, p in enumerate([100, 100, 120, 90, 130])]
    return run_backtest(_BuyAndHold(), candles, config={"initial_capital": 100000})


def test_run_backtest_is_json_safe_and_has_metrics() -> None:
    result = _run()
    json.dumps(result)  # must not raise
    assert set(result) == {"metrics", "equity_curve", "drawdown_episodes"}
    assert "total_return" in result["metrics"]
    assert "ulcer_index" in result["metrics"]


def test_bars_to_records_are_json_safe() -> None:
    candles = [_candle(0, 100), _candle(1, 101)]
    records = bars_to_records(candles)
    assert records[0]["open"] == 100.0
    assert "timestamp" in records[0]
    json.dumps(records)


def test_result_to_dict_includes_drawdown_episodes() -> None:
    result = _run()
    assert isinstance(result["drawdown_episodes"], list)


def test_backtest_rejects_unknown_strategy() -> None:
    with pytest.raises(ValueError, match="unknown strategy"):
        backtest(
            strategy="does_not_exist", symbol="RELIANCE", start="2026-01-01",
            end="2026-01-05",
        )


def test_list_strategies_returns_list() -> None:
    assert isinstance(list_strategies(), list)
