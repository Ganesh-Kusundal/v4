"""Integration tests — boot() wires extensions auto-discovery into the session.

``boot()`` must register every auto-discovered extension strategy into a
``ReactiveStrategyEngine`` and bind a ``ScannerEngine`` (market-backed) into
the session, so a session can run a discovered scanner
without touching core files.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from tradex_config.schema import AppConfig
from tradex_domain import OHLC, Candle
from tradex_domain.market import HistoricalSeries
from tradex_domain.strategy import ScannerResult
from tradex_domain.value_objects import Price, Quantity
from tradex_runtime.startup import boot, boot_context
from tradex_strategy.core.scanner import warmup_bars
from tradex_strategy.extensions import all_scanners, all_strategies
from trading.tests.risk_support import app_config, permissive_risk


def _fake_broker_with_history(closes: list[float]) -> MagicMock:
    """A broker whose ``history()`` returns candles at the given closes.

    Every requested instrument gets the same series — the pullback-scanner
    test asserts on the full universe, so the fake intentionally treats all
    universe instruments identically.
    """
    broker = MagicMock()

    def history(instrument, timeframe, start, end) -> HistoricalSeries:
        return HistoricalSeries(
            instrument=instrument,
            timeframe=timeframe,
            candles=[
                Candle(
                    instrument=instrument,
                    timeframe=timeframe,
                    ohlc=OHLC(
                        open=Price(value=Decimal(str(close))),
                        high=Price(value=Decimal(str(close))),
                        low=Price(value=Decimal(str(close))),
                        close=Price(value=Decimal(str(close))),
                    ),
                    volume=Quantity(value=Decimal("1000")),
                    timestamp=datetime(2026, 8, 1, tzinfo=UTC) + timedelta(days=i),
                )
                for i, close in enumerate(closes)
            ],
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 8, 10, tzinfo=UTC),
        )

    broker.history = history
    return broker


def _paper_with_examples() -> AppConfig:
    """A paper config that opts into the auto-discovered example strategies."""
    from dataclasses import replace

    config = AppConfig(risk=permissive_risk(), mode="paper")
    return replace(
        config,
        execution=replace(config.execution, auto_register_examples=True),
    )


class TestBootWiresDiscovery:
    """boot() registers strategies and binds scanners into the session."""

    def test_boot_registers_discovered_strategies(self, monkeypatch) -> None:
        # Examples are opt-in; this test verifies the discovery wiring, so it
        # asks for them. A default boot must not trade them.
        session = boot(
            _paper_with_examples(),
            broker=_fake_broker_with_history([10.0]),
        )
        try:
            assert session.strategy_engine is not None
            registered = session.strategy_engine.strategies  # type: ignore[union-attr]
            assert {s.strategy_id for s in all_strategies} <= set(registered)
        finally:
            session.stop()

    def test_boot_binds_scanner_engine_and_definitions(self, monkeypatch) -> None:
        session = boot(
            AppConfig(risk=permissive_risk(), mode="paper"),
            broker=_fake_broker_with_history([10.0]),
        )
        try:
            assert session._scanner_engine is not None  # noqa: SLF001 – wiring probe
            assert all_scanners  # sanity: discovery found the shipped scanners
            assert len(session._scanner_definitions) == len(all_scanners)
        finally:
            session.stop()

    def test_run_all_runs_every_discovered_scanner(self, monkeypatch) -> None:
        session = boot(
            AppConfig(risk=permissive_risk(), mode="paper"),
            broker=_fake_broker_with_history([10.0]),
        )
        try:
            results = {}
            for i, definition in enumerate(session._scanner_definitions):
                results[f"scanner_{i}"] = session._scanner_engine.run(definition)
            assert set(results) == {f"scanner_{i}" for i in range(len(all_scanners))}
            assert all(isinstance(r, list) for r in results.values())
        finally:
            session.stop()

    def test_session_runs_discovered_scanner(self, monkeypatch) -> None:
        """The pullback scanner (close > 500 AND rsi < 40) scores 2/2 on a
        falling-but-high series, proving the full path: boot → ScannerEngine
        → broker.history → AnalyticsEngine.rsi.

        The series is 20 bars because ``rsi`` declares a 14-period and an
        indicator needs one more bar than its period to emit a first value: 15
        bars minimum, 20 for margin. It used to supply 4, so the RSI was not
        warm and the condition was correctly reported as *no match* — which is
        the fix this test was accidentally asserting against, since an unwarmed
        indicator read as 0.0 matches ``rsi < 40`` on every instrument in the
        universe. So ``matched_conditions`` came back ``["close"]`` and the
        score 0.5.

        The assertion now includes the indicator's value, so a future regression
        to "unknown reads as 0.0" cannot come back through this test: an
        unwarmed RSI is absent from ``indicator_values`` altogether, not 0.0.
        """
        # Falling, but never below 500, so `close > 500` matches and a
        # strictly-falling series drives RSI to 0 — `rsi < 40` matches for the
        # real reason rather than by accident of a missing history.
        closes = [600.0 - i for i in range(20)]
        broker = _fake_broker_with_history(closes)
        session = boot(AppConfig(risk=permissive_risk(), mode="paper"), broker=broker)
        try:
            # Identify the two-condition pullback scanner by its condition
            # names (ScannerDefinition carries no name field).
            pullback = next(
                s for s in all_scanners if {c.name for c in s.conditions} == {"close", "rsi"}
            )
            # A genuinely warm indicator, not a token history — measured against
            # the scanner's own definition of warm rather than a hand-rolled
            # one, so it stays right if the rsi period changes.
            assert len(closes) >= warmup_bars(pullback.conditions)
            results = session._scanner_engine.run(pullback)
            assert isinstance(results, list)
            assert all(isinstance(r, ScannerResult) for r in results)
            assert results  # universe instruments evaluated
            for r in results:
                assert r.matched_conditions == ["close", "rsi"]
                assert r.score == 1.0
                assert r.indicator_values["rsi"] == pytest.approx(0.0, abs=1e-9)
        finally:
            session.stop()

    def test_session_runs_every_discovered_scanner(self, monkeypatch) -> None:
        session = boot(
            AppConfig(risk=permissive_risk(), mode="paper"),
            broker=_fake_broker_with_history([10.0]),
        )
        try:
            for definition in all_scanners:
                results = session._scanner_engine.run(definition)
                assert isinstance(results, list)
        finally:
            session.stop()

    def test_boot_context_carries_strategy_engine(self, monkeypatch) -> None:
        ctx = boot_context(
            AppConfig(risk=permissive_risk(), mode="paper"),
            broker=_fake_broker_with_history([10.0]),
        )
        try:
            assert ctx.strategy_engine is not None
            assert ctx.session.strategy_engine is ctx.strategy_engine
        finally:
            ctx.close()

    def test_backtest_mode_scanner_uses_datalake_provider(self, monkeypatch) -> None:
        """In backtest/replay modes the scanner market is the parquet datalake
        provider, not the broker — offline scanning over the full universe."""
        from tradex_market_data.market_provider import ParquetMarketProvider

        for mode in ("backtest", "replay"):
            session = boot(
                AppConfig(risk=permissive_risk(), mode=mode),
                broker=_fake_broker_with_history([10.0]),
            )
            try:
                engine = session._scanner_engine  # noqa: SLF001 – wiring probe
                assert isinstance(engine._market, ParquetMarketProvider)  # noqa: SLF001
            finally:
                session.stop()

    def test_backtest_mode_exposes_datalake_backtest_loader(self, monkeypatch) -> None:
        """Backtest/replay sessions expose ``session.backtest`` bound to the
        datalake loader; paper sessions keep it ``None``."""
        from tradex_market_data.backtest_loader import ParquetBacktestLoader

        for mode in ("backtest", "replay"):
            session = boot(
                AppConfig(risk=permissive_risk(), mode=mode),
                broker=_fake_broker_with_history([10.0]),
            )
            try:
                assert isinstance(session.backtest, ParquetBacktestLoader)
            finally:
                session.stop()
        session = boot(AppConfig(risk=permissive_risk(), mode="paper"))
        try:
            assert session.backtest is None
        finally:
            session.stop()


def test_paper_boot_with_real_broker_runs_discovered_scanner() -> None:
    """End-to-end with the real PaperBroker (no mocks): the paper broker's
    empty history still yields ScannerResults — proving the wiring, not the
    scoring. This is the canonical path used by ``boot()`` defaults."""
    session = boot(_CFG)
    try:
        assert session._scanner_engine is not None  # noqa: SLF001 – wiring probe
        for definition in all_scanners:
            results = session._scanner_engine.run(definition)
            assert isinstance(results, list)
    finally:
        session.stop()


#: AC-10: a paper/live boot needs a named risk policy. This suite is
#: about wiring, not risk, so it takes the permissive policy: real,
#: huge value caps, and an explicit UNBOUNDED on the two P&L guards it
#: has no mark service to satisfy.
_CFG = app_config()
