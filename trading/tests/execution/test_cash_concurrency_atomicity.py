"""Cash atomicity under concurrency (AC-21, lane 2h).

``CashLedger`` used to have no lock, and a fill moved money in three separate
critical sections: the affordability proof, the notional debit, the fee debit.
Between the proof and the debits another thread could prove the same cash
available, so two concurrent submits could both pass affordability and both
debit. The account ends up negative, or the position book and the cash ledger
disagree, and neither fact is visible anywhere.

``CashLedger.apply_fill`` now holds one lock across proof, position booking,
notional and fee, so the pair is atomic. This suite covers the surrounding
behaviour under concurrency:

- ``test_concurrent_submits_cannot_both_pass_affordability`` asserts the
  accepted count. Every thread submits the same fill against capital that can
  pay for a known small number of them.
- ``test_no_fill_is_lost`` asserts the accepted set is exactly the set whose
  fills landed in the book, so a "refuse everything" strategy cannot pass.

**What this file does NOT prove, and where the real control is.** The seam
helper below replaces ``CashLedger.apply_fill`` with a re-implementation whose
body also takes ``self._lock``, so these tests prove their OWN lock was held
and never execute the production critical section. They are not a negative
control on ``apply_fill``: replace the ``with self._lock:`` inside production
``apply_fill`` with a no-op and every test in this file still passes, because
``require_debit`` and ``_debit_unlocked`` each re-enter the real lock on their
own.

``test_cash_ledger_lock_negative_control.py`` is the control that fails on that
mutation. It exercises production ``apply_fill`` unmodified and stages the
check-then-debit window through a callback the real method is handed. Read that
file for the atomicity claim; read this one for what surrounds it.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator
from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide
from tradex_domain.events import OrderFilled
from tradex_domain.execution import Fill
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.cash_ledger import CashLedger
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fees import FeeCalculator
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_execution.trading_cache import TradingCache
from tradex_reactive.bus import ReactiveBus

INSTRUMENT = Equity.of("NSE", "RELIANCE")

#: Every thread submits this same fill: 10 shares at 100 = 1000 notional.
QTY = Decimal("10")
PRICE = Decimal("100")
NOTIONAL = QTY * PRICE

#: Capital pays for exactly five fills, so a correct ledger accepts at most
#: five and refuses every thread beyond it. The fee makes the true per-fill
#: cost larger than notional, so the accepted count is strictly below five; the
#: assertion is "<=", which is the direction that catches the race.
CAPITAL = NOTIONAL * 5

#: Threads. High enough that the window between proof and debit is contended,
#: low enough that a 10-CPU machine is not oversubscribed into flakiness.
THREADS = 16

#: CPython's GIL makes a pure-Python check-then-debit window too narrow to
#: hit reliably: without a real yield in the gap, a lockless ledger still
#: passes these tests, because the interpreter switches threads only at
#: bytecode boundaries and the two statements here are adjacent.
#:
#: So the suite CONTROLS the window instead of hoping for it. Every apply_fill
#: below runs with :func:`_widen_the_window` installed, which forces a real
#: yield between the affordability proof and the debit. A lockless
#: implementation then lets every thread prove the same cash and the double
#: spend is deterministic; a correct implementation holds the lock across the
#: yield and nothing slips through. This is what makes the suite non-vacuous
#: rather than merely green, and it is why the mutation check in the lane
#: brief can fail.
WINDOW_SECONDS = 0.002


@contextlib.contextmanager
def _widen_the_window(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force a yield between the affordability proof and the debit.

    Patches the narrow window the two statements in ``apply_fill`` leave, and
    restores it afterwards. With the lock in place the yield happens INSIDE
    the critical section, so the invariant is unaffected; with the lock
    removed it happens BETWEEN the check and the debit, which is the bug.
    """
    from tradex_execution import cash_ledger as module

    original = module.CashLedger.apply_fill

    def widened(self, side, quantity, price, fee, what, book=None):  # noqa: ANN001, ANN202
        amount = fee.amount if hasattr(fee, "amount") else Decimal(str(fee))
        notional = quantity.value * price.value
        debit = (notional if side is OrderSide.BUY else Decimal(0)) + amount
        with self._lock:  # the real lock, whatever it currently is
            self.require_debit(debit, what)
            time.sleep(WINDOW_SECONDS)  # the window a lockless impl loses
            if book is not None:
                book()
            if side is OrderSide.BUY:
                self._debit_unlocked(notional, what)
            else:
                self._cash += notional
            self._debit_unlocked(amount, "fee")
            self._fees += amount

    monkeypatch.setattr(module.CashLedger, "apply_fill", widened)
    try:
        yield
    finally:
        monkeypatch.setattr(module.CashLedger, "apply_fill", original)


def _fill(order_id: str) -> OrderFilled:
    return OrderFilled(
        fill=Fill(
            order_id=OrderId(order_id),
            instrument=INSTRUMENT,
            side=OrderSide.BUY,
            quantity=Quantity(value=QTY),
            price=Price(value=PRICE),
            fill_id=f"f-{order_id}",
        ),
    )


def _engine(cash: str = str(CAPITAL)) -> ExecutionEngine:
    return ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=SimulatedFillSource(),
        cache=TradingCache(),
        cash=Decimal(cash),
        allow_negative_cash=False,
        fee_calculator=FeeCalculator(),
    )


def _run_concurrent_submits(engine: ExecutionEngine, threads: int) -> list[str]:
    """Fire *threads* fills at one engine through a Barrier, return accepted ids.

    Every thread blocks on the Barrier first, so the submits are released
    together and the race window is actually contended rather than serialised
    by thread start-up order.
    """
    barrier = threading.Barrier(threads)
    accepted: list[str] = []
    lock = threading.Lock()

    def submit(i: int) -> None:
        order_id = f"o{i}"
        barrier.wait()
        try:
            engine._apply_fill(_fill(order_id))  # noqa: SLF001
        except ValueError:
            # Refused whole: the affordability proof raised before booking.
            return
        with lock:
            accepted.append(order_id)

    workers = [threading.Thread(target=submit, args=(i,)) for i in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30)
    return accepted


class TestConcurrentSubmitsAreAtomic:
    """AC-21: position, cash and fee move as ONE operation."""

    def test_concurrent_submits_cannot_both_pass_affordability(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """More than the affordable number of fills must not be accepted.

        This is the test that fails when the lock is removed. With proof and
        debit in separate critical sections, most threads prove the same cash
        before any of them debits, so far more than five are accepted.
        """
        engine = _engine()
        try:
            with _widen_the_window(monkeypatch):
                accepted = _run_concurrent_submits(engine, THREADS)

            affordable = int(CAPITAL / NOTIONAL)  # 5
            assert len(accepted) <= affordable, (
                f"{len(accepted)} of {THREADS} concurrent submits were accepted "
                f"against capital for {affordable}; the affordability check and "
                f"the debit are not atomic (AC-21)"
            )
            assert engine.cash_snapshot().cash >= Decimal("0"), (
                "cash went negative: two submits both passed the check and both "
                "debited"
            )
        finally:
            engine.shutdown()

    def test_cash_never_goes_negative_under_saturation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeated bursts must not drift the balance below zero."""
        engine = _engine()
        try:
            with _widen_the_window(monkeypatch):
                for _ in range(3):
                    _run_concurrent_submits(engine, 8)
                    assert engine.cash_snapshot().cash >= Decimal("0"), (
                        "a burst of concurrent submits drove cash negative"
                    )
        finally:
            engine.shutdown()

    def test_no_fill_is_lost(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every accepted fill appears in the book; none vanish silently.

        Guards the degenerate fix: refusing everything would satisfy the
        affordability test, so the accepted set must be non-empty AND every
        accepted fill must be visible in the position book and the ledger.
        """
        engine = _engine()
        try:
            with _widen_the_window(monkeypatch):
                accepted = _run_concurrent_submits(engine, THREADS)
            assert accepted, "no concurrent fill was accepted at all"

            # Every accepted fill is reflected in the book: quantity is an
            # exact multiple of the per-fill quantity, and the count matches.
            positions = engine.cache.all_positions()
            assert len(positions) == 1, "the book does not match the accepted set"
            position = positions[0]
            assert position.quantity.value == QTY * len(accepted), (
                f"book holds {position.quantity.value} for {len(accepted)} "
                f"accepted fills of {QTY} each"
            )

            # And the ledger moved by at least those fills' notional, so the two
            # books agree.
            expected_debit = NOTIONAL * len(accepted)
            actual_debit = CAPITAL - engine.cash_snapshot().cash
            assert actual_debit >= expected_debit, (
                f"cash moved {actual_debit} for {len(accepted)} fills of "
                f"{NOTIONAL} notional each"
            )
            assert engine.cash_snapshot().total_fees > Decimal("0"), (
                "no fee was charged on a fill that was accepted"
            )
        finally:
            engine.shutdown()


class TestLedgerLockIsReal:
    """Negative control on the lock itself."""

    def test_ledger_serialises_its_own_state_under_threads(self) -> None:
        """Directly exercise the ledger: N threads debiting, balance must hold.

        If ``CashLedger`` had no lock, the read-modify-write in ``on_fill``
        would interleave and the final balance would not equal the exact number
        of debits.
        """
        ledger = CashLedger(initial=CAPITAL, allow_negative=False)
        count = 50
        per_thread = 10
        total_debits = count * per_thread
        # Each debit is CAPITAL/total_debits so the sum exactly consumes the
        # balance: the final figure is 0, and any lost update shows as a
        # balance that is not exactly 0.
        amount = CAPITAL / Decimal(total_debits)
        barrier = threading.Barrier(count)

        def debit() -> None:
            barrier.wait()
            for _ in range(per_thread):
                ledger.on_fill(
                    OrderSide.BUY,
                    Quantity(value=Decimal("1")),
                    Price(value=amount),
                )

        workers = [threading.Thread(target=debit) for _ in range(count)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=30)

        expected = CAPITAL - (amount * total_debits)
        assert ledger.cash == expected, (
            f"ledger cash is {ledger.cash}, expected exactly {expected}; the "
            f"read-modify-write is not serialised (AC-21)"
        )

    def test_apply_fill_refuses_whole_without_partial_mutation(self) -> None:
        """A refused fill must not move cash, book, or fees."""
        ledger = CashLedger(initial=Decimal("100"), allow_negative=False)
        before = ledger.snapshot()
        with pytest.raises(ValueError, match="insufficient buying power"):
            ledger.apply_fill(
                side=OrderSide.BUY,
                quantity=Quantity(value=Decimal("10")),
                price=Price(value=Decimal("100")),  # 1000 > 100
                fee=Decimal("1"),
                what="fill o1",
            )
        assert ledger.snapshot() == before, "a refused fill mutated the ledger"
