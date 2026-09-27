"""Replay + backtest engine tests (F17).

Ported from v3 ``test_replay_backtest.py``.

v4 API differences:
- ``BacktestEngine(fill_source=None)`` with ``run(strategy, data)`` → ``BacktestResult``
- ``BacktestResult`` has: total_return, sharpe, max_drawdown, num_trades, trades
- No ``FakeClock`` in v4
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_domain import (
    OHLC,
    Candle,
    Equity,
    OrderSide,
    Price,
    Quantity,
    Quote,
    Signal,
    Timeframe,
)
from tradex_domain.value_objects import Price as QuotePrice
from tradex_replay.backtest import BacktestEngine, BacktestResult
from tradex_strategy.core.buy_and_hold import BuyAndHoldStrategy


def _now() -> datetime:
    return datetime(2026, 7, 31, 10, 30, tzinfo=UTC)


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _candle(close: float, ts: datetime) -> Candle:
    return Candle(
        instrument=_eq(),
        timeframe=Timeframe.D1,
        ohlc=OHLC(
            open=Price(value=Decimal(str(close - 1))),
            high=Price(value=Decimal(str(close + 1))),
            low=Price(value=Decimal(str(close - 1))),
            close=Price(value=Decimal(str(close))),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
    )


class _RecordingStrategy:
    """Strategy that records signals but never returns them from callbacks."""

    def __init__(self, signals: list[Signal]) -> None:
        self._signals = list(signals)

    @property
    def signals(self) -> list[Signal]:
        return list(self._signals)

    @property
    def strategy_id(self) -> str:
        return "recording"

    def on_bar(self, context, candle) -> None:
        pass

    def on_quote(self, context, quote) -> None:
        pass

    def on_fill(self, context, fill) -> None:
        pass


# ---------------------------------------------------------------------------
# F17 — BacktestEngine: strategy over events with metrics
# ---------------------------------------------------------------------------


class TestBacktestEngine:
    """BacktestEngine runs strategies against historical data."""

    def test_backtest_returns_result(self) -> None:
        engine = BacktestEngine()
        strategy = BuyAndHoldStrategy("bh-1", _eq())
        now = _now()
        data = [
            _candle(100.0, now - timedelta(days=2)),
            _candle(101.0, now - timedelta(days=1)),
            _candle(102.0, now),
        ]
        result = engine.run(strategy, data)
        assert isinstance(result, BacktestResult)

    def test_backtest_result_has_metrics(self) -> None:
        engine = BacktestEngine()
        strategy = BuyAndHoldStrategy("bh-1", _eq())
        now = _now()
        data = [_candle(100.0, now)]
        result = engine.run(strategy, data)
        assert isinstance(result.total_return, float)
        assert isinstance(result.sharpe, float)
        assert isinstance(result.max_drawdown, float)
        assert isinstance(result.num_trades, int)

    def test_backtest_with_quotes_counts_trades(self) -> None:
        """A signal raised on a quote still has to fill against a later bar.

        The fill reference is ``next_open``, so a quote arriving last raises a
        signal the run ends before it can fill: the candle comes after it here
        for that reason. The test used to put the quote last and expect a trade,
        which is the lookahead the reference removes.
        """
        engine = BacktestEngine()
        strategy = BuyAndHoldStrategy("bh-1", _eq())
        now = _now()
        data = [
            _candle(100.0, now),
            Quote(instrument=_eq(), ltp=QuotePrice(value=Decimal("100")), timestamp=now),
            _candle(110.0, now + timedelta(days=1)),
        ]
        result = engine.run(strategy, data)
        # BuyAndHoldStrategy emits 1 signal per quote, and there is 1 quote
        assert result.num_trades == 1
        assert result.num_unfilled == 0

    def test_backtest_empty_data(self) -> None:
        engine = BacktestEngine()
        strategy = BuyAndHoldStrategy("bh-1", _eq())
        result = engine.run(strategy, [])
        assert result.num_trades == 0

    def test_manual_strategy_signals_become_trades(self) -> None:
        """A strategy that records signals must still generate fills.

        Three bars for two signals: under the ``next_open`` reference a signal
        needs a bar after it, so two signals over two bars leaves the second one
        unfilled and excluded from ``num_trades``.
        """
        engine = BacktestEngine()
        eq = _eq()
        signals = [
            Signal(instrument=eq, direction=OrderSide.BUY, strength=1.0,
                    quantity=Quantity(value=Decimal("1")), reason="t"),
            Signal(instrument=eq, direction=OrderSide.SELL, strength=1.0,
                    quantity=Quantity(value=Decimal("1")), reason="t"),
        ]
        now = _now()
        result = engine.run(
            _RecordingStrategy(signals),
            [_candle(100.0, now), _candle(110.0, now),
             _candle(120.0, now + timedelta(days=1))],
        )
        assert result.num_trades == 2
        assert result.num_unfilled == 0
        # Fills at the next bar's open: 109.0 then 119.0.
        assert [f["price"] for f in result.fills] == [109.0, 119.0]
