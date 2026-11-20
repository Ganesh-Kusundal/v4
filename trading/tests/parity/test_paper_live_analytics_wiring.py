"""Parity: paper/live boot wires the analytics view (A3).

A strategy that declares ``required_indicators`` must receive a non-None
``context.analytics`` in every mode. ``ReactiveStrategyEngine`` now creates an
``AnalyticsRuntime`` lazily when a registered strategy needs indicators, so the
paper path matches the backtest path (which always wires one) instead of
silently falling back to hand-rolled indicator code.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_domain import OHLC, Candle, Equity, Price, Quantity, Timeframe
from tradex_reactive.bus import ReactiveBus
from tradex_replay.backtest import BacktestEngine
from tradex_runtime.session_impl import TradingSession
from tradex_strategy import ReactiveStrategyEngine
from tradex_strategy.extensions.strategies.sma_cross import SmaCrossStrategy
from trading.tests.risk_support import app_config

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _bars(n: int = 100) -> list[Candle]:
    bars: list[Candle] = []
    price = 100.0
    for i in range(n):
        price += (i % 5) - 2
        p = Price(value=Decimal(str(round(price, 2))))
        bars.append(
            Candle(
                instrument=INSTRUMENT,
                timeframe=Timeframe.M1,
                ohlc=OHLC(open=p, high=p, low=p, close=p),
                volume=Quantity(value=Decimal("1000")),
                timestamp=datetime(2026, 8, 5, 9, 15, tzinfo=UTC)
                + timedelta(minutes=i),
            )
        )
    return bars


def test_paper_live_analytics_wiring() -> None:
    bars = _bars(100)

    # Backtest run over identical bars (uses AnalyticsRuntime internally).
    strategy_bt = SmaCrossStrategy("sma_cross_bt", INSTRUMENT)
    result = BacktestEngine(
        fill_source=None,
        fee_calculator=None,
        sizer=None,
        initial_capital=Decimal("100000"),
        corporate_actions=None,
        risk_manager=None,
    ).run(strategy_bt, list(bars))
    bt_signals = list(result.trades)

    # Paper boot (production pattern: no analytics wired).
    bus = ReactiveBus()
    session = TradingSession.paper(config=app_config(), bus=bus)
    strategy_paper = SmaCrossStrategy("sma_cross_paper", INSTRUMENT)
    engine = ReactiveStrategyEngine(session.bus, session.engine.submit)
    engine.register(strategy_paper)

    views: list = []

    class _ViewProbe(SmaCrossStrategy):
        pass

    # Probe context.analytics by wrapping on_bar? Instead, monkeypatch strategy
    # to record the view it received.
    original_on_bar = strategy_paper.on_bar

    def _on_bar(context, candle):
        views.append(getattr(context, "analytics", "MISSING"))
        return original_on_bar(context, candle)

    strategy_paper.on_bar = _on_bar  # type: ignore[assignment]

    try:
        for candle in bars:
            bus.publish(candle)
        paper_signals = list(strategy_paper.signals)
    finally:
        engine.dispose_all()
        session.stop()

    # (A3) analytics must be wired.
    assert views and all(v is not None for v in views), (
        "context.analytics is None on the paper path"
    )

    # Signals must match the backtest run over identical bars.
    assert len(paper_signals) == len(bt_signals), (
        f"signal count differs: paper={len(paper_signals)} backtest={len(bt_signals)}"
    )
