"""Live boot must not run the example strategies.

``extensions/strategies/*.py`` each export a module-level instance bound to one
instrument — ``sma_cross_strategy = SmaCrossStrategy(..., RELIANCE)`` — and
``startup.boot`` registers every one of them with ``wire_strategies=True``, the
function default. There is no config key to opt out.

Verified on a real live boot: the session reaches READY with
``sma_cross_example`` wired into the engine's strategy set, and a crossover on
RELIANCE bars makes it publish a ``PlaceOrderCommand``. The BUY side is stopped
by the fail-closed cash gate, but **SELL is never cash-gated** (``risk.py:431``
gates BUY only), so an example can put on a real short in a live account.

A second defect rides along: those instances are module-level singletons, so
their indicator history and signals survive into the next ``boot()`` in the same
process.

Discovery exists so the chart/backtest route can offer strategies to a user. It
is not consent to trade with them.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock

from tradex_config.schema import AppConfig, PersistenceConfig
from tradex_domain import BrokerId, Candle, OHLC, Timeframe
from tradex_domain.events import PlaceOrderCommand
from tradex_domain.value_objects import Price, Quantity
from tradex_strategy.extensions import all_strategies


def _live_broker() -> MagicMock:
    broker = MagicMock()
    backend = MagicMock()
    backend.subscribe_orders = MagicMock()
    broker.stream_backend.return_value = backend
    broker.get_orderbook.return_value = []
    broker.get_positions.return_value = []
    broker.master_loader = None
    account = MagicMock()
    account.balance.amount = Decimal("500000")
    account.account_id.value = "acct-1"
    broker.get_account.return_value = account
    return broker


def _live_cfg(tmp_path) -> AppConfig:
    return AppConfig(
        mode="live",
        broker_id=BrokerId.DHAN,
        live_enabled=True,
        persistence=PersistenceConfig(path=str(tmp_path / "orders.db")),
    )


def test_a_live_boot_wires_no_strategies_by_default(monkeypatch, tmp_path) -> None:
    """A live account must not be trading an example nobody deployed."""
    from tradex_runtime import startup as sm

    monkeypatch.setattr(
        "tradex_runtime.live.build_broker_from_env",
        lambda *a, **k: _live_broker(),
    )
    session = sm.boot(_live_cfg(tmp_path))
    try:
        engine = getattr(session, "_strategy_engine", None) or getattr(
            session, "strategy_engine", None,
        )
        wired = list(getattr(engine, "_strategies", {}) or {})
        assert "sma_cross_example" not in wired, (
            "a live session is trading an example strategy bound to RELIANCE"
        )
        assert not any(name.endswith("_example") for name in wired), (
            f"example strategies are wired into a live account: {wired}"
        )
    finally:
        session.stop()


def test_examples_still_remain_discoverable() -> None:
    """Removing them from a live boot must not remove them from discovery.

    The chart/backtest route offers these to a user to run deliberately. The
    fix is that they are not auto-traded, not that they stop existing.
    """
    assert all_strategies, "examples must remain discoverable for the chart route"
    assert any(s.strategy_id == "sma_cross_example" for s in all_strategies)


def test_a_deliberately_enabled_strategy_is_still_registered(
    monkeypatch, tmp_path,
) -> None:
    """The gate is on auto-registration, not on strategy wiring itself."""
    from tradex_runtime import startup as sm

    class _Deployed:
        strategy_id = "deployed"
        version = "1.0.0"

        def on_bar(self, context, candle):
            return None

        def on_quote(self, context, quote):
            return None

        def on_depth(self, context, depth):
            return None

        def on_fill(self, context, fill):
            return None

    monkeypatch.setattr(
        "tradex_runtime.live.build_broker_from_env",
        lambda *a, **k: _live_broker(),
    )
    monkeypatch.setattr(
        "tradex_runtime.startup.all_strategies", (_Deployed(),),
    )
    from dataclasses import replace

    cfg = replace(_live_cfg(tmp_path), execution=replace(
        _live_cfg(tmp_path).execution, auto_register_examples=True,
    ))
    session = sm.boot(cfg)
    try:
        engine = getattr(session, "_strategy_engine", None) or getattr(
            session, "strategy_engine", None,
        )
        assert "deployed" in list(getattr(engine, "_strategies", {}) or {}), (
            "a deliberately enabled strategy must still be registered"
        )
    finally:
        session.stop()
