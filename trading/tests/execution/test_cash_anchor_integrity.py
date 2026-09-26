"""The cash anchor must be written once, and re-anchoring must be honored.

Two defects this pins:

1. Every restart appended another ``CashAccountInitialized`` to the same
   durable log, so the log grew without bound and the anchors disagreed with
   a log that should hold exactly one opening balance.
2. ``fold_cash`` took the FIRST anchor, so an operator-accepted re-anchoring
   (a deposit, a correction after reconciliation) was silently ignored on the
   next restart — the process came back with the old balance.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.events import CashAccountInitialized, OrderFilled
from tradex_execution.recovery import InMemoryEventStore, fold_cash


def _anchor(amount: str) -> CashAccountInitialized:
    return CashAccountInitialized(amount=Decimal(amount))


def test_fold_honors_a_later_re_anchoring() -> None:
    """The most recent anchor is the opening balance, not the first one.

    A deposit accepted after a reconciliation is a new opening balance. Folding
    must start from the latest one or the process resumes with a stale balance.
    """
    store = InMemoryEventStore()
    store.append(_anchor("1000"))
    store.append(_anchor("2500"))  # operator-accepted deposit

    state = fold_cash(tuple(store.replay("orders")))

    assert state.opening_cash == Decimal("2500")
    assert state.cash == Decimal("2500")


def test_a_single_anchor_folds_to_itself() -> None:
    """The ordinary case: one anchor, no fills."""
    store = InMemoryEventStore()
    store.append(_anchor("1000"))

    state = fold_cash(tuple(store.replay("orders")))

    assert state.cash == Decimal("1000")
    assert state.opening_cash == Decimal("1000")


def test_a_corrected_anchor_discards_the_fills_before_it() -> None:
    """A re-anchoring restates the balance; earlier fills are already in it.

    Opening 1000, spending 1000, then anchoring 2500 (a deposit) must fold to
    2500. Re-applying the pre-anchor fill would yield 1500 — the deposit would
    silently swallow a real trade.
    """
    from tradex_domain.enums import OrderSide
    from tradex_domain.execution import Fill
    from tradex_domain.instruments import Equity
    from tradex_domain.value_objects import OrderId, Price, Quantity
    store = InMemoryEventStore()
    store.append(_anchor("1000"))
    store.append(
        OrderFilled(
            fill=Fill(
                order_id=OrderId("o1"),
                instrument=Equity.of("NSE", "RELIANCE"),
                side=OrderSide.BUY,
                quantity=Quantity(value=Decimal("10")),
                price=Price(value=Decimal("100")),
                fill_id="f1",
            ),
        ),
    )
    store.append(_anchor("2500"))  # operator-accepted deposit

    assert fold_cash(tuple(store.replay("orders"))).cash == Decimal("2500")


def test_a_fill_bearing_log_without_an_anchor_is_not_anchored_from_broker_funds(
    monkeypatch, tmp_path,
) -> None:
    """Anchoring a fill-bearing log double-counts every historical fill.

    The broker's current balance is already net of those fills. Using it as the
    opening balance and then replaying the fills subtracts them twice, so the
    rebuild reports a balance the account never had. Cash must stay unknown
    instead, and the fail-closed gate keeps denying.
    """
    from tradex_config.schema import AppConfig, PersistenceConfig
    from tradex_domain import BrokerId
    from tradex_domain.enums import OrderSide, OrderType, TimeInForce
    from tradex_domain.events import PlaceOrderCommand
    from tradex_domain.execution import OrderRequest
    from tradex_domain.instruments import Equity
    from tradex_domain.value_objects import Price, Quantity
    from tradex_execution.sqlite_event_store import SQLiteEventStore

    from tradex_trading.runtime import startup as sm

    db = str(tmp_path / "orders.db")
    cfg = AppConfig(
        mode="paper",
        broker_id=BrokerId.PAPER,
        live_enabled=False,
        persistence=PersistenceConfig(path=db),
    )

    # A session that produced fills but whose anchor never persisted.
    session = sm.boot(cfg)
    try:
        session.bus.publish(
            PlaceOrderCommand(
                request=OrderRequest(
                    instrument=Equity.of("NSE", "RELIANCE"),
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    quantity=Quantity(value=Decimal("10")),
                    price=Price(value=Decimal("100")),
                    time_in_force=TimeInForce.DAY,
                ),
            ),
        )
    finally:
        session.stop()

    # Drop the anchor, leaving a log that has fills and no opening balance.
    import sqlite3

    con = sqlite3.connect(db)
    con.execute("DELETE FROM events WHERE event_type = 'CashAccountInitialized'")
    con.commit()
    con.close()

    second = sm.boot(cfg)
    try:
        store = SQLiteEventStore(db)
        try:
            anchors = [
                e for e in store.replay("orders")
                if isinstance(e, CashAccountInitialized)
            ]
        finally:
            store.close()
        assert anchors == [], (
            "a log that already holds fills must not be anchored from broker "
            "funds; that balance is net of the fills and replaying them again "
            "double-counts the history"
        )
    finally:
        second.stop()


def test_restart_does_not_append_a_second_anchor(monkeypatch, tmp_path) -> None:
    """A restart must not grow the log with another opening balance.

    Recovery re-seeds the engine from the fold; that is a restore, not a new
    opening balance, and appending one both duplicates state and would let a
    later correction be masked by the stale first anchor.
    """
    from tradex_config.schema import AppConfig, PersistenceConfig
    from tradex_domain import BrokerId
    from tradex_execution.sqlite_event_store import SQLiteEventStore

    from tradex_trading.runtime import startup as sm

    db = str(tmp_path / "orders.db")
    cfg = AppConfig(
        mode="paper",
        broker_id=BrokerId.PAPER,
        live_enabled=False,
        persistence=PersistenceConfig(path=db),
    )

    for expected_anchors in (1, 1, 1):
        session = sm.boot(cfg)
        try:
            assert session.engine.cash_snapshot().cash == Decimal("100000")
        finally:
            session.stop()
        store = SQLiteEventStore(db)
        try:
            anchors = [
                e for e in store.replay("orders")
                if isinstance(e, CashAccountInitialized)
            ]
            assert len(anchors) == expected_anchors, (
                f"expected {expected_anchors} opening anchor(s), found "
                f"{len(anchors)} — a restart must not re-anchor the account"
            )
        finally:
            store.close()
