"""F-45 / F-46 / F-47: the batched MEDIUM fixes, each with its negative control.

Three separate defects, one suite, because each is a few lines and each is the
kind that stays invisible: a documented sweep that never runs, a default that
discards what it is given, and a counter that cannot name its owner.

Negative controls (each fails when its fix is removed):
  * F-45 ``test_pending_max_age_is_wired_at_boot`` / ``_expiries_are_counted``
  * F-46 ``test_memory_store_round_trips_a_layout`` — a per-call connection
    gives a *new empty database* every call, so this returns None.
  * F-47 ``test_order_failures_are_attributed_to_the_strategy`` — a global
    counter cannot answer "which strategy".
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from tradex_config.schema import AppConfig
from tradex_domain import BrokerId
from tradex_domain.enums import ExchangeId, Timeframe
from tradex_domain.instruments import Equity
from tradex_interfaces.workspace_store import WorkspaceStore
from tradex_runtime.startup import boot
from trading.tests.risk_support import app_config, permissive_risk

_INSTRUMENT = Equity.of(ExchangeId.NSE, "RELIANCE")


# ---------------------------------------------------------------------------
# F-45 — deferred-order expiry is actually configured
# ---------------------------------------------------------------------------


def test_pending_max_age_is_wired_at_boot() -> None:
    """F-45 negative control: the engine default of 0 disables expiry.

    ``ReactiveStrategyEngine`` defaults ``pending_max_age_bars=0``, documented
    as "no expiry". The composition root never passed it, so the max-age sweep
    was a no-op in production — a deferred ``next_open`` order could sit in the
    queue forever. The config default is non-zero, so this fails if the wiring
    is dropped.
    """
    session = boot(
        app_config(mode="paper", risk=permissive_risk(), feed_instruments=(_INSTRUMENT,))
    )
    try:
        assert session.strategy_engine._pending_max_age_bars > 0, (
            "expiry is disabled: startup.py did not pass "
            "pending_max_age_bars= to ReactiveStrategyEngine"
        )
    finally:
        session.stop()


def test_pending_max_age_is_configurable_and_validated() -> None:
    """F-45: the bound is configuration, and 0 is a reachable explicit opt-out."""
    from tradex_config.schema import ExecutionConfig

    assert ExecutionConfig().pending_max_age_bars > 0
    assert ExecutionConfig(pending_max_age_bars=0).pending_max_age_bars == 0
    with pytest.raises(ValueError):
        ExecutionConfig(pending_max_age_bars=-1)


def test_a_deferred_order_that_ages_out_is_cancelled_and_counted() -> None:
    """F-45: the sweep runs, and an expired order is counted for its strategy.

    Live had no equivalent of the backtest's ``num_unfilled``; this is the
    number that makes the two comparable.
    """
    from tradex_domain.events import OrderCancelled
    from tradex_reactive.bus import ReactiveBus
    from tradex_strategy.core.engine import ReactiveStrategyEngine

    bus = ReactiveBus()
    # max_age=1: the order survives one bar and is cancelled on the second.
    engine = ReactiveStrategyEngine(
        bus, submit=lambda request: None, pending_max_age_bars=1
    )
    strategy = _SignalsOnce()
    engine.register(strategy)
    cancelled: list[Any] = []
    bus.of_type(OrderCancelled).subscribe(cancelled.append)

    # Bar 1 for RELIANCE defers the order. Bars 2-3 for TCS are a *different*
    # instrument, so the order is never filled — it only ages. That is the
    # shape the sweep exists for: a signal on a symbol that stops ticking.
    bus.publish(_candle(1))
    other = Equity.of(ExchangeId.NSE, "TCS")
    for day in (2, 3, 4):
        bus.publish(_candle(day, instrument=other))

    assert cancelled, "no deferred order was cancelled by the max-age sweep"
    stats = engine.order_stats(strategy.strategy_id)
    assert stats["orders_expired"] >= 1, (
        "the order was cancelled but not counted, so live cannot be compared "
        "to the backtest's num_unfilled"
    )


# ---------------------------------------------------------------------------
# F-46 — WorkspaceStore holds one connection
# ---------------------------------------------------------------------------


def test_memory_store_round_trips_a_layout() -> None:
    """F-46 negative control: a per-call connection is a new empty database.

    ``sqlite3.connect(":memory:")`` inside every method meant the default
    ``db_path`` returned ``None`` on every read and dropped every write. The
    store's docstring claimed the opposite. This asserts the round trip.
    """
    store = WorkspaceStore()  # the default :memory: path
    try:
        revision, conflicted = store.put("layout-1", {"symbol": "RELIANCE"}, 0, False)
        assert not conflicted
        assert revision == 1
        row = store.get("layout-1")
        assert row is not None, "the write did not survive: :memory: is not shared"
        assert row["data"] == {"symbol": "RELIANCE"}
        assert [r["layout_id"] for r in store.list()] == ["layout-1"]
    finally:
        store.close()


def test_memory_store_sees_a_write_from_a_second_store_view() -> None:
    """F-46: the point of one connection is that reads and writes share state."""
    store = WorkspaceStore()
    try:
        store.put("l", {"a": 1}, None, False)
        store.put("l", {"a": 2}, 1, False)
        assert store.get("l")["data"] == {"a": 2}
        assert store.get("l")["revision"] == 2
    finally:
        store.close()


def test_file_store_still_works_and_close_is_idempotent(tmp_path: Any) -> None:
    """F-46: the real-path path the default server actually uses is unchanged."""
    path = tmp_path / "nested" / "ws.sqlite"
    store = WorkspaceStore(path)
    store.put("l", {"x": 1}, None, False)
    store.close()
    store.close()  # idempotent

    reopened = WorkspaceStore(path)
    try:
        assert reopened.get("l")["data"] == {"x": 1}
    finally:
        reopened.close()


def test_delete_removes_the_layout() -> None:
    store = WorkspaceStore()
    try:
        store.put("l", {"x": 1}, None, False)
        store.delete("l")
        assert store.get("l") is None
    finally:
        store.close()


# ---------------------------------------------------------------------------
# F-47 — order failures name their strategy
# ---------------------------------------------------------------------------


def test_order_failures_are_attributed_to_the_strategy() -> None:
    """F-47 negative control: a global counter cannot answer "which strategy".

    Live published ``ErrorOccurred`` and bumped one session counter, so a
    strategy whose every order is refused was indistinguishable from a healthy
    one. The backtest re-raised and aborted, so this was the live-only half of
    the parity gap.
    """
    from tradex_reactive.bus import ReactiveBus
    from tradex_strategy.core.engine import ReactiveStrategyEngine

    def _refuse(request: Any) -> Any:
        raise RuntimeError("venue down")

    bus = ReactiveBus()
    engine = ReactiveStrategyEngine(bus, submit=_refuse)
    failing = _SignalsOnce("failing")
    other = _SignalsOnce("other")
    engine.register(failing)
    engine.register(other)

    bus.publish(_candle(1))

    assert engine.order_stats("failing")["order_failures"] >= 1
    assert engine.order_stats("other")["order_failures"] == 0, (
        "a failure was attributed to a strategy that did not fail"
    )


def test_a_dropped_order_with_no_submit_is_counted() -> None:
    """F-47: submit=None is also a failure, not a silent return."""
    from tradex_reactive.bus import ReactiveBus
    from tradex_strategy.core.engine import ReactiveStrategyEngine

    bus = ReactiveBus()
    engine = ReactiveStrategyEngine(bus, submit=None)
    strategy = _SignalsOnce()
    engine.register(strategy)

    # next_open defers, so the order reaches _place() on the SECOND bar.
    bus.publish(_candle(1))
    bus.publish(_candle(2))

    assert engine.order_stats(strategy.strategy_id)["order_failures"] >= 1


def test_a_clean_strategy_reports_zero() -> None:
    from tradex_reactive.bus import ReactiveBus
    from tradex_strategy.core.engine import ReactiveStrategyEngine

    bus = ReactiveBus()
    engine = ReactiveStrategyEngine(bus, submit=lambda request: None)
    strategy = _SignalsOnce()
    engine.register(strategy)

    bus.publish(_candle(1))

    stats = engine.order_stats(strategy.strategy_id)
    assert stats == {"order_failures": 0, "orders_expired": 0}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _SignalsOnce:
    """Emits one BUY on its first bar, then stays quiet."""

    def __init__(self, strategy_id: str = "f45-probe") -> None:
        self.strategy_id = strategy_id
        self.version = "1.0.0"
        self._fired = False

    def on_bar(self, ctx: Any, candle: Any) -> Any:
        if self._fired:
            return None
        self._fired = True
        from tradex_domain import Signal
        from tradex_domain.enums import OrderSide

        # No strategy_id: the engine attributes the signal to the registered
        # strategy that returned it, which is exactly the attribution F-47 is
        # about counting.
        return Signal(
            instrument=candle.instrument,
            direction=OrderSide.BUY,
            strength=1.0,
            reason="probe",
        )

    def on_quote(self, ctx: Any, quote: Any) -> None:
        return None

    def on_depth(self, ctx: Any, depth: Any) -> None:
        return None

    def on_fill(self, ctx: Any, fill: Any) -> None:
        return None


def _candle(day: int, instrument: Any = _INSTRUMENT) -> Any:
    from datetime import UTC, datetime

    from tradex_domain import OHLC, Candle
    from tradex_domain.value_objects import Price, Quantity

    price = Price(value=Decimal("100"))
    return Candle(
        instrument=instrument,
        timeframe=Timeframe.M1,
        ohlc=OHLC(open=price, high=price, low=price, close=price),
        volume=Quantity(value=Decimal("100")),
        timestamp=datetime(2026, 9, 24, 9, 15 + day, tzinfo=UTC),
    )


def test_boot_uses_a_paper_broker_by_default() -> None:
    """Guard so the suite's boot assumptions stay honest."""
    assert AppConfig().broker_id is BrokerId.PAPER
