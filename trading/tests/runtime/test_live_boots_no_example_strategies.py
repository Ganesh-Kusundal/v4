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

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from tradex_config.schema import AppConfig, PersistenceConfig
from tradex_domain import BrokerId
from tradex_strategy.extensions import all_strategies
from trading.tests.risk_support import permissive_risk

# These tests verify strategy filtering, so they need the real strategy list.
pytestmark = pytest.mark.no_test_strategy


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
    return AppConfig(risk=permissive_risk(),
        mode="live",
        broker_id=BrokerId.DHAN,
        live_enabled=True,
        persistence=PersistenceConfig(path=str(tmp_path / "orders.db")),
    )


def test_a_live_boot_refuses_with_no_named_strategy(
    named_tape, monkeypatch, tmp_path,
) -> None:
    from tradex_runtime.startup import boot

    monkeypatch.setattr(
        "tradex_runtime.live.build_broker_from_env",
        lambda *a, **k: _live_broker(),
    )
    with pytest.raises(ValueError, match="TRADEX_STRATEGY_IDS"):
        boot(_live_cfg(tmp_path))


def test_examples_still_remain_discoverable() -> None:
    """Removing them from a live boot must not remove them from discovery.

    The chart/backtest route offers these to a user to run deliberately. The
    fix is that they are not auto-traded, not that they stop existing.
    """
    assert all_strategies, "examples must remain discoverable for the chart route"
    assert any(s.strategy_id == "sma_cross_example" for s in all_strategies)


def test_a_deliberately_enabled_strategy_is_still_registered(
    named_tape, monkeypatch, tmp_path,
) -> None:
    """The gate is on auto-registration, not on strategy wiring itself."""
    from dataclasses import replace

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
    cfg = replace(_live_cfg(tmp_path), execution=replace(
        _live_cfg(tmp_path).execution,
        strategy_ids=("deployed",),
        auto_register_examples=False,
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


def _paper_cfg(tmp_path) -> AppConfig:
    return AppConfig(risk=permissive_risk(),
        mode="paper",
        broker_id=BrokerId.DHAN,
        persistence=PersistenceConfig(path=str(tmp_path / "orders.db")),
    )


def _registered_strategy_ids(session) -> set:
    reg = session.strategy_registry()
    return {rt.artifact.strategy_id for rt in reg.list_all()}


def test_per_binding_definition_suppresses_module_twin(
    named_tape, monkeypatch, tmp_path,
) -> None:
    from dataclasses import replace

    from tradex_runtime import startup as sm

    monkeypatch.setattr(
        "tradex_runtime.live.build_broker_from_env",
        lambda *a, **k: _live_broker(),
    )
    cfg = replace(_live_cfg(tmp_path), execution=replace(
        _live_cfg(tmp_path).execution,
        strategy_ids=("sma_cross",),
        auto_register_examples=False,
    ))
    session = sm.boot(cfg)
    try:
        registered = _registered_strategy_ids(session)
        assert "sma_cross:RELIANCE" in registered, (
            "per-binding instance must be registered when a tape is named"
        )
        assert "sma_cross_example" not in registered, (
            "module twin must be suppressed when the definition binds it"
        )
    finally:
        session.stop()


def test_module_twin_registered_without_feed_instruments(
    no_tape, tmp_path,
) -> None:
    from dataclasses import replace

    from tradex_runtime.startup import boot

    cfg = replace(_paper_cfg(tmp_path), execution=replace(
        _paper_cfg(tmp_path).execution, auto_register_examples=True,
    ))
    session = boot(cfg, feed_instruments=())
    try:
        registered = _registered_strategy_ids(session)
        assert "sma_cross_example" in registered, (
            "module twin must register when no tape is named"
        )
        assert "sma_cross:RELIANCE" not in registered, (
            "per-binding instance must not register without a tape"
        )
    finally:
        session.stop()


def test_a_live_boot_wires_no_unnamed_example(
    named_tape, monkeypatch, tmp_path,
) -> None:
    from dataclasses import replace

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
        "tradex_runtime.live.build_broker_from_env", lambda *a, **k: _live_broker()
    )
    monkeypatch.setattr(
        "tradex_runtime.startup.all_strategies", (_Deployed(), *_all_examples())
    )
    cfg = _live_cfg(tmp_path)
    cfg = replace(cfg, execution=replace(cfg.execution, strategy_ids=("deployed",)))
    session = sm.boot(cfg)
    try:
        engine = session.strategy_engine
        wired = set(getattr(engine, "_strategies", {}) or {})
        assert wired == {"deployed"}
    finally:
        session.stop()


def _all_examples():
    from tradex_strategy.extensions import all_strategies

    return tuple(all_strategies)
