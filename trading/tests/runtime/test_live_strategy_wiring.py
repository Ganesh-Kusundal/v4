"""Live mode must wire strategies by default.

A live session with zero strategies is a no-op: it connects to the broker,
streams quotes, but never trades. This test pins the contract that live
mode boots with wire_strategies=True unless explicitly overridden, and that
a live boot with no strategies named via TRADEX_STRATEGY_IDS fails closed.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from decimal import Decimal

import pytest
from tradex_brokers.paper import PaperBroker
from tradex_config.schema import AppConfig, PersistenceConfig
from tradex_domain import BrokerId
from tradex_runtime.startup import boot
from trading.tests.risk_support import permissive_risk


def _live_cfg(
    tmp_path,
    *,
    auto_register_examples: bool = False,
    strategy_ids: tuple[str, ...] = (),
) -> AppConfig:
    """A live config with a real PaperBroker injected (no MagicMock)."""
    cfg = AppConfig(
        risk=permissive_risk(),
        mode="live",
        broker_id=BrokerId.DHAN,
        live_enabled=True,
        persistence=PersistenceConfig(path=str(tmp_path / "orders.db")),
    )
    return replace(
        cfg,
        execution=replace(
            cfg.execution,
            auto_register_examples=auto_register_examples,
            strategy_ids=strategy_ids,
        ),
    )


def test_live_mode_wires_strategies_by_default() -> None:
    """boot() with mode='live' must default wire_strategies to True."""
    sig = inspect.signature(boot)
    assert sig.parameters["wire_strategies"].default is True


def test_live_boot_without_strategies_raises(
    named_tape, tmp_path,
) -> None:
    """A live boot with wire_strategies=True but no strategies must fail.

    The ValueError is raised BEFORE broker.connect(), so we don't need a
    working broker transport — the strategy check fires first.
    """
    config = _live_cfg(tmp_path)
    with pytest.raises(ValueError, match="TRADEX_STRATEGY_IDS"):
        boot(config, wire_strategies=True, broker=PaperBroker())


def test_live_boot_refuses_the_example_auto_register_switch(
    named_tape, tmp_path,
) -> None:
    config = _live_cfg(tmp_path, auto_register_examples=True)
    with pytest.raises(ValueError, match="auto_register_examples"):
        boot(config, wire_strategies=True, broker=PaperBroker())
