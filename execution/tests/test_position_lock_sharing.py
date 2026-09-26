"""Regression: a committed fill must survive a concurrent quote-driven re-mark.

Defect (Critical, M-1): ``MarkToMarketService.on_quote`` performed a
read-modify-write on the position book with no lock, while
``PositionAccountant.on_fill`` held a *per-instrument* lock from its own
private lock map. Because ``startup.py`` hands the mark service the bare
``TradingCache`` and the engine builds a separate ``PositionAccountant`` over
that same cache, the two components guarded one book with two unrelated
locks. A quote arriving between the mark service's read and its write
clobbered a concurrently-committed fill, reverting the position to a
pre-fill quantity.

The interleaving is forced deterministically rather than raced for: a gated
cache pauses the mark thread inside ``get_position`` — after the read, before
the write — while the fill commits on another thread.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill, Position
from tradex_domain.instruments import Equity
from tradex_domain.market import Quote
from tradex_domain.value_objects import Money, Price, Quantity
from tradex_execution.mark_to_market import MarkToMarketService
from tradex_execution.position_accountant import PositionAccountant
from tradex_execution.trading_cache import TradingCache

_INSTRUMENT = Equity.of("NSE", "RELIANCE")
_T0 = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)


class _GatedCache(TradingCache):
    """Cache that pauses the *victim* thread after its position read.

    ``on_quote`` reads the position and then writes a new one built from that
    read. Pausing in between is exactly the window in which a concurrent fill
    is lost, so this makes the defect deterministic instead of probabilistic.
    """

    def __init__(self) -> None:
        super().__init__()
        self.victim: threading.Thread | None = None
        self.read_done = threading.Event()
        self.fill_committed = threading.Event()
        #: How long the mark thread yields after its read. Long enough for the
        #: unfixed code to let a concurrent fill land in the window (the fill
        #: returns immediately there), short enough that the fixed code — where
        #: the fill correctly *blocks* on the position lock — is not stalled.
        self.yield_seconds = 0.25

    def get_position(self, instrument):  # type: ignore[override]
        position = super().get_position(instrument)
        if threading.current_thread() is self.victim:
            self.read_done.set()
            self.fill_committed.wait(timeout=self.yield_seconds)
        return position


def _fill(qty: str = "5", price: str = "2500") -> Fill:
    return Fill(
        order_id="order-1",
        instrument=_INSTRUMENT,
        side=OrderSide.BUY,
        quantity=Quantity(Decimal(qty)),
        price=Price(Decimal(price)),
        timestamp=_T0,
    )


def _quote(price: str = "2510", offset_seconds: int = 1) -> Quote:
    return Quote(
        instrument=_INSTRUMENT,
        ltp=Price(Decimal(price)),
        bid=Price(Decimal(price)),
        ask=Price(Decimal(price)),
        timestamp=_T0.replace(second=offset_seconds),
    )


def _seed(cache: TradingCache) -> None:
    cache.update_position(
        Position(
            instrument=_INSTRUMENT,
            quantity=Quantity(Decimal("5")),
            avg_price=Price(Decimal("2500")),
            realized_pnl=Money(amount=Decimal("0")),
            unrealized_pnl=Money(amount=Decimal("0")),
        )
    )


def test_committed_fill_survives_remark_that_read_before_it_landed() -> None:
    """The mark read position 5; a fill took it to 10; the write must not undo that."""
    cache = _GatedCache()
    accountant = PositionAccountant(cache)
    service = MarkToMarketService(cache)
    _seed(cache)

    def do_remark() -> None:
        service.on_quote(_quote())

    marker = threading.Thread(target=do_remark, name="mark")
    cache.victim = marker
    marker.start()

    assert cache.read_done.wait(timeout=10), "mark thread never reached its position read"

    def do_fill() -> None:
        accountant.on_fill(_fill(qty="5"))
        cache.fill_committed.set()

    filler = threading.Thread(target=do_fill, name="fill")
    filler.start()
    filler.join(timeout=10)
    assert not filler.is_alive(), "fill thread deadlocked against the position lock"

    marker.join(timeout=10)
    assert not marker.is_alive(), "mark thread deadlocked"

    final = cache.get_position(_INSTRUMENT)
    assert final is not None
    assert final.quantity.value == Decimal("10"), (
        "concurrent re-mark destroyed a committed fill: "
        f"book={final.quantity.value}, expected 10 (5 seeded + 5 filled)"
    )


def test_two_instruments_do_not_contend() -> None:
    """The per-instrument lock must stay per-instrument after the fix."""
    cache = TradingCache()
    a = Equity.of("NSE", "AAA")
    b = Equity.of("NSE", "BBB")
    for instrument in (a, b):
        cache.update_position(
            Position(
                instrument=instrument,
                quantity=Quantity(Decimal("1")),
                avg_price=Price(Decimal("100")),
                realized_pnl=Money(amount=Decimal("0")),
                unrealized_pnl=Money(amount=Decimal("0")),
            )
        )
    assert cache.position_lock(a) is not cache.position_lock(b)
    assert cache.position_lock(a) is cache.position_lock(a), "lock must be stable per key"
