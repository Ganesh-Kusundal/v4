"""C2 — reconcile before session.start().

The principal-architect review (C2) found that startup.py called
``session.start()`` (line 456) BEFORE reconciliation (lines 461-503).
The strategy engine is already subscribed; a critical drift
discovered during reconciliation can leave the session trading on
a diverged book.

The fix: extract reconciliation into a helper, run it BEFORE
``session.start()``, and trip the kill switch (refuse to start) if
critical drift is found.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from tradex_domain import BrokerId
from tradex_trading.execution.reconciliation import DriftItem, DriftSeverity
from tradex_trading.runtime import startup as startup_mod
from tradex_trading.config.schema import AppConfig, PersistenceConfig


def _patched_boot_with_drift(
    monkeypatch, drift_items: list[DriftItem], tmp_path
):
    """Build a live-mode boot with a fake broker that returns a fixed drift.

    Returns a tuple (cfg, broker).
    """
    broker = MagicMock()
    broker.capabilities = MagicMock()
    broker.capabilities.max_stream_instruments = 1000
    broker.capabilities.depth_levels = 0
    broker.get_orderbook.return_value = []
    broker.get_positions.return_value = []
    backend = MagicMock()
    backend.subscribe_orders = MagicMock()
    broker.stream_backend = MagicMock(return_value=backend)
    broker.master_loader = None
    broker.connect = MagicMock()

    def fake_reconcile(self, local, broker_positions_arg):
        return list(drift_items)

    monkeypatch.setattr(
        "tradex_trading.runtime.live.build_broker_from_env",
        lambda _pid, **_kw: broker,
    )
    monkeypatch.setattr(
        "tradex_trading.execution.engine.ReconciliationEngine.reconcile",
        fake_reconcile,
    )
    cfg = AppConfig(mode="live", broker_id=BrokerId.DHAN, live_enabled=True,
                    persistence=PersistenceConfig(path=str(tmp_path / "orders.db")))
    return cfg, broker


def test_critical_drift_prevents_session_from_reaching_ready(monkeypatch, tmp_path) -> None:
    """RED: a critical drift at startup must leave session.state != READY.

    Before the fix: session.start() runs first, then reconciliation
    trips the kill switch, but the session is already READY.
    After the fix: reconciliation runs first, the session is left
    in NEW state (or stopped) when critical drift is detected.
    """
    drift = DriftItem(
        kind="position",
        key="NSE:RELIANCE",
        severity=DriftSeverity.CRITICAL,
        reason="local 100 vs broker 0",
    )
    cfg, _broker = _patched_boot_with_drift(monkeypatch, [drift], tmp_path)

    # Should not raise, but should not be READY either.
    session = startup_mod.boot(cfg)
    try:
        assert session.state.value != "READY", (
            "Critical drift must prevent the session from reaching READY "
            "(C2: reconcile before session.start())"
        )
        # The kill switch must be tripped.
        assert session.engine.kill_switch is True
    finally:
        try:
            session.stop()
        except Exception:
            pass


def test_no_drift_lets_session_reach_ready(monkeypatch, tmp_path) -> None:
    """With no drift, the session still reaches READY (regression guard)."""
    cfg, _broker = _patched_boot_with_drift(monkeypatch, [], tmp_path)

    session = startup_mod.boot(cfg)
    try:
        assert session.state.value == "READY"
        assert session.engine.kill_switch is False
    finally:
        session.stop()


def test_low_drift_does_not_block_ready(monkeypatch, tmp_path) -> None:
    """Only HIGH/CRITICAL drift must block; LOW/MEDIUM may proceed with a log."""
    drift = DriftItem(
        kind="position",
        key="NSE:RELIANCE",
        severity=DriftSeverity.LOW,
        reason="minor qty diff",
    )
    cfg, _broker = _patched_boot_with_drift(monkeypatch, [drift], tmp_path)

    session = startup_mod.boot(cfg)
    try:
        assert session.state.value == "READY", (
            "LOW drift must not block the session; only HIGH/CRITICAL does"
        )
    finally:
        session.stop()
