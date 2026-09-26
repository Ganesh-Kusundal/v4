"""A restarted process must rebuild its book, and stay halted until it agrees.

Two claims, both about real money after a crash:

  1. The durable event log is authoritative — a fresh process rebuilds the
     same positions and cash the previous one held, from the log alone.
  2. Boot refuses READY while the rebuilt book disagrees with the broker.
     A recovered book that has not been reconciled is not a trusted book.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from tradex_config.schema import AppConfig, PersistenceConfig
from tradex_domain import BrokerId
from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.execution import OrderRequest, Position
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Money, Price, Quantity

from tradex_trading.sdk.session import SessionState

INSTRUMENT = Equity.of("NSE", "RELIANCE")
#: The balance the paper book holds after the ladder. A live broker's funds
#: reflect the fills the log recorded, so a broker that still reports the
#: opening figure is showing genuine drift — which now halts the boot.
RECOVERED_CASH = Decimal("99440")


def _broker(
    *,
    cash: Decimal = Decimal("1000000"),
    positions: list | None = None,
    orderbook: list | None = None,
) -> MagicMock:
    broker = MagicMock()
    backend = MagicMock()
    backend.subscribe_orders = MagicMock()
    broker.stream_backend.return_value = backend
    broker.get_orderbook.return_value = orderbook or []
    broker.get_positions.return_value = positions or []
    broker.master_loader = None
    account = MagicMock()
    account.balance.amount = cash
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


def _cfg(tmp_path) -> AppConfig:
    """A durable session that actually produces fills.

    ``mode="paper"`` fills locally at the requested price — a mocked live venue
    cannot — and is the one mode that carries a real cash ledger, which is what
    the recovery fold has to restore. Persistence is configured, so the event
    store, the recovery fold, and the boot ordering under test are the same code
    the live path runs.
    """
    return AppConfig(
        mode="paper",
        broker_id=BrokerId.PAPER,
        live_enabled=False,
        persistence=PersistenceConfig(path=str(tmp_path / "orders.db")),
    )


def _run_trades(session) -> None:
    """Submit a ladder through the session's engine.

    Replay/simulated fills are used rather than a broker submission: this test
    is about what a restarted process recovers from the durable log, and a
    mocked live venue cannot produce real fills. The recovery code under test
    (``recover_trading_cache``) is identical for every mode.
    """
    from tradex_domain.events import PlaceOrderCommand

    for side, qty, price in [
        (OrderSide.BUY, "10", "100"),
        (OrderSide.SELL, "4", "110"),
    ]:
        session.bus.publish(
            PlaceOrderCommand(
                request=OrderRequest(
                    instrument=INSTRUMENT,
                    side=side,
                    order_type=OrderType.LIMIT,
                    quantity=Quantity(value=Decimal(qty)),
                    price=Price(value=Decimal(price)),
                    time_in_force=TimeInForce.DAY,
                ),
            ),
        )


def test_restart_rebuilds_positions_and_cash_from_the_durable_log(
    monkeypatch, tmp_path,
) -> None:
    """A second process recovers the book the first one held."""
    from tradex_trading.runtime import startup as sm

    cfg = _cfg(tmp_path)
    first = sm.boot(cfg)
    try:
        _run_trades(first)
        before = first.engine.cache.all_positions()
        assert before, "the ladder should have opened a position"
        cash_before = first.engine.cash_snapshot().cash
        # The fold must actually carry a balance. Without this, a recovery that
        # silently returned zero on both sides would compare equal and pass.
        assert cash_before not in (None, Decimal("0")), (
            "the live session must hold a non-zero cash balance to recover"
        )
    finally:
        first.stop()

    # A fresh process over the SAME persistence file, twice. A single restart
    # cannot see state that accumulates per boot (a growing cash anchor, a
    # duplicated order), which is exactly the class of bug this pins.
    for _attempt in range(2):
        second = sm.boot(cfg)
        try:
            after = second.engine.cache.all_positions()
            assert after, "the restarted process recovered no position"
            assert [p.quantity.value for p in after] == [
                p.quantity.value for p in before
            ]
            assert [p.avg_price.value for p in after] == [
                p.avg_price.value for p in before
            ]
            recovered_cash = second.engine.cash_snapshot().cash
            assert recovered_cash == cash_before, (
                "the restarted process must recover the balance it held "
                f"before, not {recovered_cash}"
            )
        finally:
            second.stop()


def test_boot_refuses_ready_when_recovered_book_drifts_from_the_broker(
    monkeypatch, tmp_path,
) -> None:
    """Recovery is not trust: a drifted book must not reach READY.

    The reconciliation gate is live-only (a paper broker has no independent
    book to disagree with), so this drives the live boot path directly. The
    recovered book comes from a durable log written by an earlier session.
    """
    from tradex_trading.runtime import startup as sm

    # Session 1 writes a durable book.
    durable = _cfg(tmp_path)
    first = sm.boot(durable)
    try:
        _run_trades(first)
    finally:
        first.stop()

    # Session 2 boots LIVE over the same log; the broker reports a position
    # the recovered book does not have.
    drifted = Position(
        instrument=INSTRUMENT,
        quantity=Quantity(value=Decimal("999")),
        avg_price=Price(value=Decimal("1")),
        realized_pnl=Money(amount=Decimal("0")),
        unrealized_pnl=Money(amount=Decimal("0")),
    )
    monkeypatch.setattr(
        "tradex_runtime.live.build_broker_from_env",
        lambda *a, **k: _broker(
            positions=[drifted], cash=RECOVERED_CASH,
        ),
    )
    second = sm.boot(_live_cfg(tmp_path))
    try:
        assert second.state != SessionState.READY, (
            "a recovered book that disagrees with the broker must not be READY"
        )
        assert second.engine.kill_switch is True
    finally:
        second.stop()


def test_boot_reaches_ready_when_broker_agrees_with_the_recovered_book(
    monkeypatch, tmp_path,
) -> None:
    """The gate must not be stuck shut: agreement still reaches READY."""
    from tradex_trading.runtime import startup as sm

    # Session 1 writes a durable book.
    first = sm.boot(_cfg(tmp_path))
    try:
        _run_trades(first)
        recovered = first.engine.cache.all_positions()
        # Whatever this book actually holds is what a live broker would report.
        recovered_cash = first.engine.cash_snapshot().cash
    finally:
        first.stop()

    assert recovered, "the ladder should have produced a position to reconcile"
    broker_positions = [
        Position(
            instrument=p.instrument,
            quantity=p.quantity,
            avg_price=p.avg_price,
            realized_pnl=Money(amount=Decimal("0")),
            unrealized_pnl=Money(amount=Decimal("0")),
        )
        for p in recovered
    ]
    # The broker must also know about the orders the log recorded, otherwise
    # orderbook reconciliation reports them as missing and halts the boot.
    broker_orders = [o for o in first.engine.cache.all_orders() if o is not None]
    monkeypatch.setattr(
        "tradex_runtime.live.build_broker_from_env",
        lambda *a, **k: _broker(
            positions=broker_positions,
            orderbook=broker_orders,
            cash=recovered_cash,
        ),
    )
    second = sm.boot(_live_cfg(tmp_path))
    try:
        assert second.state == SessionState.READY
    finally:
        second.stop()
