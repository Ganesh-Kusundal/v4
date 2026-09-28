"""WS-E contract tests: TradingCache — ported from v3.

Adapted for v4 API: update_order/update_position (not set_*),
string-keyed lookups, no snapshot/restore.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain import (
    Equity,
    Money,
    Order,
    OrderId,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Price,
    Quantity,
    TimeInForce,
)
from tradex_execution.trading_cache import TradingCache


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _order(order_id: str = "o-1", status: OrderStatus = OrderStatus.PENDING) -> Order:
    return Order(
        order_id=OrderId(value=order_id),
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=status,
    )


def _position() -> Position:
    return Position(
        instrument=_eq(),
        quantity=Quantity(value=Decimal("10")),
        avg_price=Price(value=Decimal("100")),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
    )


def test_order_update_and_get_by_str() -> None:
    cache = TradingCache()
    cache.update_order(_order("o-1"))
    assert cache.get_order("o-1") is not None
    assert cache.get_order("missing") is None


def test_all_orders_returns_insertion_set() -> None:
    cache = TradingCache()
    cache.update_order(_order("o-1"))
    cache.update_order(_order("o-2"))
    assert {o.order_id.value for o in cache.all_orders()} == {"o-1", "o-2"}


def test_position_keyed_by_instrument_id() -> None:
    cache = TradingCache()
    cache.update_position(_position())
    # update_position now uses _instrument_key (instrument_id), consistent
    # with set_position and get_position.
    assert cache.get_position(_eq()).quantity.value == 10
    assert cache.get_position(str(_eq().instrument_id)).quantity.value == 10
    assert cache.get_position("NSE:TCS") is None


def test_clear_drops_everything() -> None:
    cache = TradingCache()
    cache.update_order(_order("o-1"))
    cache.update_position(_position())
    cache.clear()
    assert cache.get_order("o-1") is None
    assert cache.get_position(_eq()) is None


def test_order_update_overwrites() -> None:
    cache = TradingCache()
    cache.update_order(_order("o-1", OrderStatus.PENDING))
    cache.update_order(_order("o-1", OrderStatus.FILLED))
    order = cache.get_order("o-1")
    assert order is not None
    assert order.status is OrderStatus.FILLED


def test_set_order_alias() -> None:
    cache = TradingCache()
    cache.set_order(_order("o-1"))
    assert cache.get_order("o-1") is not None


def test_get_order_by_order_id() -> None:
    cache = TradingCache()
    cache.update_order(_order("o-1"))
    oid = OrderId(value="o-1")
    assert cache.get_order(oid) is not None


def test_set_position_and_get_by_instrument() -> None:
    cache = TradingCache()
    cache.set_position(_position())
    eq = _eq()
    # get_position accepts Instrument, InstrumentId, or str
    assert cache.get_position(eq) is not None
    assert cache.get_position(str(eq.instrument_id)) is not None


def test_snapshot_and_restore() -> None:
    cache = TradingCache()
    cache.set_order(_order("o-1"))
    cache.set_position(_position())
    snap = cache.snapshot()

    cache.clear()
    assert cache.get_order("o-1") is None

    cache.restore(snap)
    assert cache.get_order("o-1") is not None
    assert cache.get_position(_eq()) is not None


def test_instrument_key_variants() -> None:
    eq = _eq()
    # str passthrough
    assert TradingCache._instrument_key("RELIANCE") == "RELIANCE"
    # InstrumentId
    assert TradingCache._instrument_key(eq.instrument_id) == str(eq.instrument_id)
    # Instrument
    assert TradingCache._instrument_key(eq) == str(eq.instrument_id)


def _pos(symbol: str, qty: str) -> Position:
    return Position(
        instrument=Equity.of("NSE", symbol),
        quantity=Quantity(value=Decimal(qty)),
        avg_price=Price(value=Decimal("100")),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
    )


# ---------------------------------------------------------------------------
# set_positions: batch write (P0-F6) — the adoption apply's atomic seam
# ---------------------------------------------------------------------------


def test_set_positions_writes_every_row() -> None:
    cache = TradingCache()
    cache.set_positions([_pos("RELIANCE", "1"), _pos("TCS", "2")])
    assert cache.get_position("NSE:RELIANCE").quantity.value == Decimal("1")
    assert cache.get_position("NSE:TCS").quantity.value == Decimal("2")


def test_set_positions_validates_before_writing_anything() -> None:
    """A bad row anywhere in the batch leaves the book untouched.

    The adoption plan is all-or-nothing semantics (a partial apply reports a
    correction it did not make), so the batch write must not land half its
    rows before raising.
    """
    cache = TradingCache()
    cache.set_position(_pos("RELIANCE", "1"))
    with pytest.raises(TypeError):
        cache.set_positions([_pos("TCS", "2"), "not-a-position"])
    assert cache.get_position("NSE:TCS") is None
    assert cache.get_position("NSE:RELIANCE").quantity.value == Decimal("1")


def test_set_positions_takes_instrument_locks_in_sorted_key_order() -> None:
    """Lock acquisition is sorted-key order: deadlock-free against the fill
    path's per-instrument RMW and against any other batch apply.

    The contract pinned here: ``set_positions`` hands the lock helper its keys
    in sorted order and acquires the returned locks in that order (the helper
    preserves key order), so acquisition order never depends on caller order.
    """

    class _Recording(TradingCache):
        def __init__(self) -> None:
            super().__init__()
            self.lock_keys: list[str] = []

        def _instrument_locks(self, positions: list[Position]) -> list:
            self.lock_keys.extend(
                self._instrument_key(p.instrument) for p in positions
            )
            return super()._instrument_locks(positions)

    cache = _Recording()
    cache.set_positions([_pos("TCS", "2"), _pos("RELIANCE", "1"), _pos("INFY", "3")])
    assert cache.lock_keys == sorted(cache.lock_keys)
    assert cache.lock_keys == ["NSE:INFY", "NSE:RELIANCE", "NSE:TCS"]


def test_set_positions_cannot_interleave_with_a_fill_rmw() -> None:
    """The lost-update hole (P0-F6): a fill holds ``position_lock`` across its
    read-modify-write; a batch apply running under only the dict lock slips
    between the fill's read and write and is then clobbered (or clobbers).

    Deterministic via events: the fill holds the instrument lock, then the
    apply starts. With the lock discipline the apply blocks until the fill's
    RMW lands; the fill's increment is never lost.
    """
    import threading

    cache = TradingCache()
    instrument = Equity.of("NSE", "RELIANCE")
    cache.set_position(_pos("RELIANCE", "0"))

    hold = threading.Event()
    entered = threading.Event()
    errors: list[BaseException] = []

    def _fill_rmw() -> None:
        try:
            with cache.position_lock(instrument):
                entered.set()
                hold.wait(timeout=2.0)
                existing = cache.get_position(instrument)
                base = existing.quantity.value if existing is not None else Decimal("0")
                cache.set_position(_pos("RELIANCE", str(base + 1)))
        except BaseException as exc:  # noqa: BLE001 - surfaced via errors
            errors.append(exc)

    apply_done = threading.Event()

    def _apply() -> None:
        try:
            cache.set_positions([_pos("RELIANCE", "100")])
        except BaseException as exc:  # noqa: BLE001 - surfaced via errors
            errors.append(exc)
        finally:
            apply_done.set()

    fill_thread = threading.Thread(target=_fill_rmw)
    fill_thread.start()
    assert entered.wait(timeout=2.0)

    apply_thread = threading.Thread(target=_apply)
    apply_thread.start()

    # While the fill holds the instrument lock, the apply must still be blocked.
    apply_done.wait(timeout=0.2)
    assert not apply_done.is_set(), (
        "set_positions must block on the per-instrument lock: the fill's "
        "read-modify-write is in flight"
    )

    hold.set()
    fill_thread.join(timeout=2.0)
    apply_thread.join(timeout=2.0)
    assert errors == []

    # Serialised either way: fill +1 lands (0+1=1), then the apply's 100 wins
    # last — or the apply first and the fill's RMW sees 100 (101). The lost
    # update would show the fill's 1 over the apply's 100 or vice versa with
    # the other write vanished; the blocked-apply assertion above is the pin.
    final = cache.get_position("NSE:RELIANCE").quantity.value
    assert final in (Decimal("100"), Decimal("101"))
