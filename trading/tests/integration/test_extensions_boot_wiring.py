"""Integration tests — boot() wires extensions auto-discovery into the session.

``boot()`` must register every auto-discovered extension strategy into a
``ReactiveStrategyEngine`` and bind a ``ScannerEngine`` (market-backed) into
the session's ``ScannerService``, so a session can run a discovered scanner
without touching core files.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

from tradex_domain import OHLC, Candle
from tradex_domain.market import HistoricalSeries
from tradex_domain.strategy import ScannerResult
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.config.schema import AppConfig
from tradex_trading.runtime.startup import boot, boot_context
from tradex_trading.strategy.extensions import all_scanners, all_strategies


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


class TestBootWiresDiscovery:
    """boot() registers strategies and binds scanners into the session."""

    def test_boot_registers_discovered_strategies(self, monkeypatch) -> None:
        from tradex_brokers import BrokerFactory

        monkeypatch.setattr(
            BrokerFactory, "create", lambda _bid, **_kw: _fake_broker_with_history([10.0])
        )
        session = boot(AppConfig(mode="paper"))
        try:
            assert session.strategy_engine is not None
            registered = session.strategy_engine.strategies  # type: ignore[union-attr]
            assert {s.strategy_id for s in all_strategies} <= set(registered)
        finally:
            session.stop()

    def test_boot_binds_scanner_engine_and_definitions(self, monkeypatch) -> None:
        from tradex_brokers import BrokerFactory

        monkeypatch.setattr(
            BrokerFactory, "create", lambda _bid, **_kw: _fake_broker_with_history([10.0])
        )
        session = boot(AppConfig(mode="paper"))
        try:
            assert session.scanner._engine is not None  # noqa: SLF001 – wiring probe
            assert all_scanners  # sanity: discovery found the shipped scanners
            assert len(session.scanner.discovered) == len(all_scanners)
        finally:
            session.stop()

    def test_run_all_runs_every_discovered_scanner(self, monkeypatch) -> None:
        from tradex_brokers import BrokerFactory

        monkeypatch.setattr(
            BrokerFactory, "create", lambda _bid, **_kw: _fake_broker_with_history([10.0])
        )
        session = boot(AppConfig(mode="paper"))
        try:
            results = session.scanner.run_all()
            assert set(results) == {f"scanner_{i}" for i in range(len(all_scanners))}
            assert all(isinstance(r, list) for r in results.values())
        finally:
            session.stop()

    def test_session_runs_discovered_scanner(self, monkeypatch) -> None:
        """The pullback scanner (close > 500 AND rsi < 40) scores 2/2 on a
        falling-but-high series, proving the full path: boot → ScannerService
        → ScannerEngine → broker.history → AnalyticsEngine.rsi."""
        from tradex_brokers import BrokerFactory

        broker = _fake_broker_with_history([600.0, 590.0, 580.0, 570.0])
        monkeypatch.setattr(BrokerFactory, "create", lambda _bid, **_kw: broker)
        session = boot(AppConfig(mode="paper"))
        try:
            # Identify the two-condition pullback scanner by its condition
            # names (ScannerDefinition carries no name field).
            pullback = next(
                s for s in all_scanners if {c.name for c in s.conditions} == {"close", "rsi"}
            )
            results = session.scanner.run(pullback)
            assert isinstance(results, list)
            assert all(isinstance(r, ScannerResult) for r in results)
            assert results  # universe instruments evaluated
            for r in results:
                assert r.matched_conditions == ["close", "rsi"]
                assert r.score == 1.0
        finally:
            session.stop()

    def test_session_runs_every_discovered_scanner(self, monkeypatch) -> None:
        from tradex_brokers import BrokerFactory

        monkeypatch.setattr(
            BrokerFactory, "create", lambda _bid, **_kw: _fake_broker_with_history([10.0])
        )
        session = boot(AppConfig(mode="paper"))
        try:
            for definition in all_scanners:
                results = session.scanner.run(definition)
                assert isinstance(results, list)
        finally:
            session.stop()

    def test_boot_context_carries_strategy_engine(self, monkeypatch) -> None:
        from tradex_brokers import BrokerFactory

        monkeypatch.setattr(
            BrokerFactory, "create", lambda _bid, **_kw: _fake_broker_with_history([10.0])
        )
        ctx = boot_context(AppConfig(mode="paper"))
        try:
            assert ctx.strategy_engine is not None
            assert ctx.session.strategy_engine is ctx.strategy_engine
        finally:
            ctx.close()

    def test_backtest_mode_scanner_uses_datalake_provider(self, monkeypatch) -> None:
        """In backtest/replay modes the scanner market is the parquet datalake
        provider, not the broker — offline scanning over the full universe."""
        from tradex_brokers import BrokerFactory

        from tradex_trading.datalake.market_provider import ParquetMarketProvider

        monkeypatch.setattr(
            BrokerFactory, "create", lambda _bid, **_kw: _fake_broker_with_history([10.0])
        )
        for mode in ("backtest", "replay"):
            session = boot(AppConfig(mode=mode))
            try:
                engine = session.scanner._engine  # noqa: SLF001 – wiring probe
                assert isinstance(engine._market, ParquetMarketProvider)  # noqa: SLF001
            finally:
                session.stop()

    def test_backtest_mode_exposes_datalake_backtest_loader(self, monkeypatch) -> None:
        """Backtest/replay sessions expose ``session.backtest`` bound to the
        datalake loader; paper sessions keep it ``None``."""
        from tradex_brokers import BrokerFactory

        from tradex_trading.datalake.backtest_loader import ParquetBacktestLoader

        monkeypatch.setattr(
            BrokerFactory, "create", lambda _bid, **_kw: _fake_broker_with_history([10.0])
        )
        for mode in ("backtest", "replay"):
            session = boot(AppConfig(mode=mode))
            try:
                assert isinstance(session.backtest, ParquetBacktestLoader)
            finally:
                session.stop()
        session = boot(AppConfig(mode="paper"))
        try:
            assert session.backtest is None
        finally:
            session.stop()


def test_paper_boot_with_real_broker_runs_discovered_scanner() -> None:
    """End-to-end with the real PaperBroker (no mocks): the paper broker's
    empty history still yields ScannerResults — proving the wiring, not the
    scoring. This is the canonical path used by ``boot()`` defaults."""
    session = boot()
    try:
        assert session.scanner._engine is not None  # noqa: SLF001 – wiring probe
        for definition in all_scanners:
            results = session.scanner.run(definition)
            assert isinstance(results, list)
    finally:
        session.stop()
