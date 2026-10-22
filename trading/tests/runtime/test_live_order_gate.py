"""The adapter's own order wall must follow `live_orders_enabled`."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from unittest.mock import MagicMock

from tradex_config.schema import AppConfig, PersistenceConfig
from tradex_domain import BrokerId
from tradex_runtime.startup import boot
from trading.tests.risk_support import permissive_risk


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


def _broker() -> MagicMock:
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


def _cfg(tmp_path, *, orders: bool) -> AppConfig:
    cfg = AppConfig(
        risk=permissive_risk(),
        mode="live",
        broker_id=BrokerId.DHAN,
        live_enabled=True,
        live_orders_enabled=orders,
        persistence=PersistenceConfig(path=str(tmp_path / "state.db")),
        execution=replace(
            AppConfig().execution, strategy_ids=("deployed",)
        ),
    )
    return cfg


def _boot_and_capture(monkeypatch, named_tape, tmp_path, *, orders: bool):
    seen: dict = {}

    def fake_build(provider, **kwargs):
        seen.update(kwargs)
        return _broker()

    monkeypatch.setattr(
        "tradex_runtime.live.build_broker_from_env", fake_build
    )
    monkeypatch.setattr("tradex_runtime.startup.all_strategies", (_Deployed(),))
    session = boot(_cfg(tmp_path, orders=orders))
    session.stop()
    return seen


def test_the_order_gate_is_closed_by_default(monkeypatch, named_tape, tmp_path) -> None:
    seen = _boot_and_capture(monkeypatch, named_tape, tmp_path, orders=False)
    assert seen["allow_order_operations"] is False


def test_the_order_gate_opens_only_when_configured(monkeypatch, named_tape, tmp_path) -> None:
    seen = _boot_and_capture(monkeypatch, named_tape, tmp_path, orders=True)
    assert seen["allow_order_operations"] is True
