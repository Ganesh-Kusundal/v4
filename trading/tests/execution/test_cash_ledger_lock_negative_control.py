"""A non-vacuous negative control on the ``CashLedger.apply_fill`` lock (AC-21).

**Why this file exists.** An earlier version of the concurrency suite widened
the check-then-debit window by monkeypatching ``CashLedger.apply_fill`` with its
own re-implementation whose body also took ``self._lock``. The suite therefore
proved only that its *own* lock was held and never executed the production
critical section. Proof of the defect: replacing the ``with self._lock:``
inside production ``apply_fill`` with a no-op left all 28 tests across the five
cash suites green. A control that cannot fail is not a control.

**The rule this file keeps.** ``CashLedger.apply_fill`` runs as written.
Nothing here substitutes, wraps or re-implements it, and nothing replaces
``CashLedger._lock`` either: the lock under test has to be the real object.
The window is staged through callbacks the real method already invokes, and no
production code was changed to make that possible.

**The seams.** ``apply_fill`` takes its lock once and then calls two things it
was handed, both *between* the affordability proof and the debits and both
inside the critical section:

- the ``book`` callback, which the engine uses to book the position, and
- -- on the inbound door only, where the engine itself holds
  ``CashLedger.transaction()`` around the call -- the engine's durable
  ``_event_store.append``, a pure I/O boundary that in production is a SQLite
  WAL write.

Parking a submitting thread in either one is what a blocked I/O call does in
production, so the test stages the same thing instead of hoping to hit it. Under
CPython's GIL the pure-Python gap between the proof and the debit is a few
bytecodes wide and a lockless ledger passes a naive race test anyway; the window
has to be widened deliberately or the control proves nothing.

**What each control asserts, and why it fails without the lock.** Two things,
checked separately, because either alone can pass vacuously:

1. *the window was staged and was real* -- the seam was actually entered and
   actually held the thread for the whole park interval, and
2. *the window was closed* -- no two submitting threads were ever inside the
   seam at once (the caller holds the lock across it), no more fills were
   accepted than the capital pays for, every accepted fill is visible in the
   position book, and a refused fill left no trace at all.

Assertion 1 is what makes assertion 2 mean something: a park no thread ever
reached would leave the ledger serialised and every other assertion true.

**The two guards, and which control sees which.** The critical section has two
owners and this file covers both, because each covers a gap the other has:

- ``CashLedger.apply_fill``'s own ``with self._lock:``. ``apply_fill`` is called
  on the inbound door *inside* ``CashLedger.transaction()``, so that door stays
  serialised even if ``apply_fill`` loses its lock. The booking-seam test
  therefore calls ``apply_fill`` directly, with no engine and no outer lock, and
  that is the test that fails on the required mutation. With
  ``with self._lock:`` replaced by a no-op it reports 8 of 8 submitters inside
  the seam at once, and 8 fills booked against 3 accepted.
- the caller's ``CashLedger.transaction()``, which the engine holds across the
  store append. The booking-seam test cannot see this one at all, so the store
  seam covers it, with a second arm that removes the guard to show the staging
  detects its loss.

Both guards exist in production and both are load-bearing; neither is weakened
here. Nothing in this file replaces either.

**Running the mutation from a git worktree (a silent FALSE PASS).** The shared
venv at ``v4/.venv`` has this workspace installed *editable*, pointed at the
MAIN checkout. So ``python -m pytest`` launched from a worktree imports
``tradex_execution`` from the MAIN checkout, not from the worktree: a mutation
applied inside the worktree is never loaded, and a control that should fail
reports green. It looks exactly like a working control, which is the worst
failure mode there is.

Check before trusting a mutation run:

    python -c "import tradex_execution.cash_ledger as m; print(m.__file__)"

If that prints a path outside the worktree, shadow the install by putting
the worktree's source roots ahead of the editable ones. ``PYTHONPATH`` is
colon-separated, and the order matters (``execution/src`` first, then
``domain/src``, ``reactive/src``, ``brokers/src``), so build
it per-root rather than as one long literal:

    W=<worktree>
    export PYTHONPATH="$W/execution/src:$W/domain/src:$W/reactive/src"
    export PYTHONPATH="$PYTHONPATH:$W/brokers/src"

Re-run the check and confirm it now names the worktree file. Do this before
reading any result as evidence, and re-check after any environment change:
a control that passes for the wrong reason is indistinguishable from one that
passes for the right reason.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from decimal import Decimal
from typing import NamedTuple

from tradex_domain.enums import OrderSide
from tradex_domain.events import OrderFilled
from tradex_domain.execution import Fill
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.cash_ledger import CashLedger
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fees import FeeCalculator
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_execution.recovery import InMemoryEventStore
from tradex_execution.trading_cache import TradingCache
from tradex_reactive.bus import ReactiveBus

INSTRUMENT = Equity.of("NSE", "RELIANCE")

#: One thread's fill: 10 shares at 100 = 1000 notional. Every thread submits
#: this same fill, so the count accepted is bounded by what the capital pays for.
QTY = Decimal("10")
PRICE = Decimal("100")
NOTIONAL = QTY * PRICE

#: Threads released together by a Barrier, so the window is contended rather
#: than serialised by thread start-up order.
THREADS = 8

#: How long a submitter is parked in the seam. The engine and the ledger each
#: cross their seam once per fill, so this is the entire cost of staging.
PARK_SECONDS = 0.01

#: Poll period of the thread watching for two submitters inside one seam. A
#: correct ledger never lets a second one in, so on a healthy tree this just
#: polls for the length of the burst and finds nothing.
POLL_SECONDS = 0.0002

#: Ceiling on the submit phase, so a regression that deadlocks or livelocks
#: fails the suite instead of hanging CI.
TIMEOUT_SECONDS = 60.0


class _SeamReport(NamedTuple):
    """What the staged window actually did."""

    #: How many times the seam was entered. Zero would mean the park was never
    #: reached and every assertion made against it is meaningless.
    entered: int
    #: The most threads ever inside the seam at the same moment. 1 on a correct
    #: ledger -- the caller holds the lock across the seam -- and THREADS on a
    #: lockless one, which is the whole defect in one number.
    max_concurrent: int
    #: Longest a single call sat in the seam. Proves the yield was real.
    max_hold: float


class _StagedSeam:
    """A callback the real fill path invokes, parked to stage the window.

    Wrapping a callback ``apply_fill`` is *handed* -- never the method itself --
    keeps the control honest: the code under test is untouched, and the seam is
    a real thing production calls, sitting between the affordability proof and
    the debits and inside the critical section.
    """

    def __init__(self) -> None:
        self._inside: set[int] = set()
        self._guard = threading.Lock()
        self._gate = threading.Barrier(1)  # parks the first caller; nobody waits
        self._stop = threading.Event()
        self._entered = 0
        self._max_concurrent = 0
        self._max_hold = 0.0
        self._watcher = threading.Thread(target=self._watch, daemon=True)
        self._watcher.start()

    def _watch(self) -> None:
        while not self._stop.is_set():
            with self._guard:
                self._max_concurrent = max(self._max_concurrent, len(self._inside))
            time.sleep(POLL_SECONDS)

    def __call__(self) -> None:
        """Park the calling thread, as a blocked store write would."""
        ident = threading.get_ident()
        with self._guard:
            self._inside.add(ident)
            self._entered += 1
        self._gate.wait(timeout=TIMEOUT_SECONDS)
        started = time.monotonic()
        try:
            time.sleep(PARK_SECONDS)
        finally:
            with self._guard:
                self._inside.discard(ident)
                self._max_hold = max(self._max_hold, time.monotonic() - started)

    def report(self) -> _SeamReport:
        self._stop.set()
        self._watcher.join(timeout=5)
        with self._guard:
            return _SeamReport(self._entered, self._max_concurrent, self._max_hold)


class _Booking(NamedTuple):
    """The ``book`` callback, the seam it parks in, and what it recorded."""

    callback: Callable[[], None]
    seam: _StagedSeam
    booked: list[str]


def _transaction_without_the_lock() -> Callable[..., AbstractContextManager[None]]:
    """``transaction()`` with its lock elided, to arm the second burst below.

    This is the regression the first arm is a control for. It is used only
    inside that one test's second arm, and only to show the staging detects the
    loss -- the armed arm, which is the control, runs against production code
    exactly as written.
    """

    @contextmanager
    def transaction(self: CashLedger) -> Iterator[None]:
        del self
        yield

    return transaction


def _booked_seam() -> _Booking:
    """A ``book`` callback that parks in the seam and records that it ran.

    Recording *inside* the seam callback is the point: a fill that reached
    ``book()`` is a fill that passed the affordability proof, so this list is
    exactly the set of fills the proof admitted. The debits run after it.
    """
    seam = _StagedSeam()
    booked: list[str] = []
    lock = threading.Lock()

    def book() -> None:
        seam()
        with lock:
            booked.append(f"book-{threading.get_ident()}")

    return _Booking(book, seam, booked)


def _assert_window_was_staged(report: _SeamReport) -> None:
    """The seam really was reached and really did hold a thread open.

    Without this the controls below could pass on a harness that never staged
    anything: the ledger would be serialised for reasons that have nothing to do
    with the lock, and the numbers would agree by accident.
    """
    assert report.entered > 0, (
        "no submit ever reached the seam between the proof and the debits, so "
        "the window this control stages was never opened"
    )
    assert report.max_hold >= PARK_SECONDS, (
        f"the seam held a submitter for {report.max_hold:.6f}s, less than the "
        f"{PARK_SECONDS}s park: the window is not being staged"
    )


def _assert_window_was_closed(report: _SeamReport) -> None:
    """No two submitters were ever inside the seam at the same moment.

    The seam sits inside ``apply_fill``'s critical section, so a correct ledger
    admits one thread and refuses the rest. This is the assertion that is
    deterministic rather than statistical: with ``apply_fill``'s
    ``with self._lock:`` replaced by a no-op, every submitter piles in and this
    reports :data:`THREADS`.
    """
    assert report.max_concurrent == 1, (
        f"{report.max_concurrent} submits were inside the same seam at once, "
        f"from {report.entered} that reached it; ``apply_fill`` is not holding "
        f"its lock across the proof and the debits (AC-21)"
    )


def _burst(submit: Callable[[str], None], threads: int) -> tuple[list[str], list[str]]:
    """Release *threads* submits together.

    Returns (accepted order ids, refused order ids). A submit is accepted when
    it returned without a refusal. Anything other than the expected
    ``ValueError`` is re-raised here rather than swallowed, so a crash cannot be
    mistaken for a refusal.
    """
    barrier = threading.Barrier(threads)
    accepted: list[str] = []
    refused: list[str] = []
    lock = threading.Lock()

    def submitter(i: int) -> None:
        order_id = f"o{i}"
        barrier.wait(timeout=TIMEOUT_SECONDS)
        try:
            submit(order_id)
        except ValueError:
            with lock:
                refused.append(order_id)
            return
        with lock:
            accepted.append(order_id)

    workers = [threading.Thread(target=submitter, args=(i,)) for i in range(threads)]
    for w in workers:
        w.start()
    deadline = time.monotonic() + TIMEOUT_SECONDS
    for w in workers:
        w.join(timeout=max(0.0, deadline - time.monotonic()))
    alive = [w for w in workers if w.is_alive()]
    assert not alive, (
        f"{len(alive)} of {threads} submits never finished within "
        f"{TIMEOUT_SECONDS}s; the ledger is not making progress"
    )
    return accepted, refused


def _assert_not_over_subscribed(accepted: list[str], refused: list[str], affordable: int) -> None:
    """Some fills applied, the rest were refused, and never more than the limit.

    A correct ledger accepts at most *affordable* of :data:`THREADS` concurrent
    submits and refuses the rest. It must also accept *some*: "refuse everything"
    satisfies the ceiling on its own, and it is exactly the shape a broken lock
    can fall into.
    """
    assert accepted, (
        f"all {THREADS} concurrent submits were refused; a ledger that accepts "
        f"nothing satisfies the ceiling below and proves nothing"
    )
    assert len(accepted) + len(refused) == THREADS, (
        f"{len(accepted)} accepted plus {len(refused)} refused is not "
        f"{THREADS}; a submit neither applied nor refused"
    )
    assert len(accepted) <= affordable, (
        f"{len(accepted)} of {THREADS} concurrent submits were accepted against "
        f"a limit of {affordable}; the affordability check and the debit are "
        f"not atomic (AC-21)"
    )


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


def _logged_fills(store: InMemoryEventStore) -> list[OrderFilled]:
    return [e for e in store.replay("orders") if isinstance(e, OrderFilled)]


class TestApplyFillThroughTheInboundDoor:
    """``ExecutionEngine._apply_fill`` -> production ``apply_fill``.

    The engine door takes the ledger lock itself (``CashLedger.transaction()``)
    across proof, durable append, booking and the debits, so the seam parked
    here -- ``_event_store.append``, a pure I/O boundary -- is the closest thing
    to a real blocking call between the proof and the debits.
    """
    #: Capital pays for three notionals. The real fee calculator makes the true
    #: per-fill cost larger than that, so a correct ledger accepts at most two
    #: and refuses the other six.
    CAPITAL = NOTIONAL * 3
    AFFORDABLE = int(CAPITAL / NOTIONAL)  # 3

    def _engine(self, store: InMemoryEventStore) -> ExecutionEngine:
        return ExecutionEngine(
            bus=ReactiveBus(),
            fill_source=SimulatedFillSource(),
            cache=TradingCache(),
            event_store=store,
            cash=self.CAPITAL,
            allow_negative_cash=False,
            fee_calculator=FeeCalculator(),
        )

    def test_the_lock_spans_the_durability_seam_between_proof_and_debit(self) -> None:
        """One submitter at a time crosses the seam, and the window was staged.

        ``apply_fill`` holds its lock across the store append, so the second
        submitter is still blocked at the lock and never gets in. Deterministic,
        not statistical.
        """
        report = self._staged_burst()

        _assert_window_was_staged(report)
        _assert_window_was_closed(report)

    def test_the_caller_holds_the_ledger_lock_across_its_own_durable_append(self) -> None:
        """The engine's own ``transaction()`` spans the seam, and it is load-bearing.

        ``_apply_fill`` wraps this whole door -- proof, append, dedup, booking,
        debits -- in ``CashLedger.transaction()``, so the submitter parked in
        the store is holding the ledger lock, not merely standing next to it.
        That is a second, independent guard on the same critical section, and the
        booking-seam test above cannot see it, because with the caller's guard in
        place the burst is still serialised no matter what ``apply_fill`` does.

        Both arms run on the same input, the same staging and the same burst, so
        the first cannot rot into passing by luck:

        - **armed**, the seam admits one submitter at a time, and
        - **guard removed**, the very same staging lets all eight in at once.

        The second arm is the reason the first means something: it is a direct
        demonstration that this staging can see the loss of the caller-side
        guard, so a green first arm is evidence and not just agreement.
        """
        armed = self._staged_burst()
        _assert_window_was_staged(armed)
        _assert_window_was_closed(armed)

        real_transaction = CashLedger.transaction
        try:
            CashLedger.transaction = _transaction_without_the_lock()  # type: ignore[method-assign]
            opened = self._staged_burst()
        finally:
            CashLedger.transaction = real_transaction  # type: ignore[method-assign]

        _assert_window_was_staged(opened)
        assert opened.max_concurrent > 1, (
            f"with ``CashLedger.transaction()`` disabled the same staging still "
            f"admitted one submitter at a time ({opened.max_concurrent} of "
            f"{opened.entered} that reached it); the seam cannot see the loss of "
            f"the caller-side lock, so the armed arm above proves nothing"
        )

    def _staged_burst(self) -> _SeamReport:
        """One burst of eight submits through the inbound door, seam parked."""
        store = InMemoryEventStore()
        engine = self._engine(store)
        seam = _StagedSeam()
        real_append = store.append
        store.append = lambda event: (seam(), real_append(event))[1]  # type: ignore[method-assign]
        try:
            _burst(lambda order_id: engine._apply_fill(_fill(order_id)), THREADS)  # noqa: SLF001
        finally:
            engine.shutdown()
        return seam.report()

    def test_a_refused_fill_books_nothing_and_reaches_no_log(self) -> None:
        """Refused whole, under concurrency, on the real engine door.

        ``apply_fill`` promises this in its own contract: the proof runs before
        ``book()``, so a fill that cannot be afforded books nothing and moves no
        money. Under concurrency it is the invariant a lost lock breaks first --
        the proof stops being atomic with the debits, and a fill that the debits
        then refuse has already booked a position and already been logged.

        This door also holds ``CashLedger.transaction()`` across the call, so it
        is the door that pins *that* guarantee as well as the book/log/cash
        agreement. It cannot by itself detect the loss of ``apply_fill``'s own
        lock -- ``transaction()`` would keep serialising the burst -- which is
        why :class:`TestCashLedgerApplyFillDirectly` exists. With the engine's
        ``transaction()`` neutered *instead*, this door reports 8 books against
        3 accepted.
        """
        store = InMemoryEventStore()
        engine = self._engine(store)
        seam = _StagedSeam()
        real_append = store.append
        store.append = lambda event: (seam(), real_append(event))[1]  # type: ignore[method-assign]
        try:
            accepted, refused = _burst(
                lambda order_id: engine._apply_fill(_fill(order_id)), THREADS  # noqa: SLF001
            )
            _assert_window_was_staged(seam.report())
            _assert_not_over_subscribed(accepted, refused, self.AFFORDABLE)

            positions = engine.cache.all_positions()
            booked = sum(p.quantity.value for p in positions)
            cash = engine.cash_snapshot().cash
        finally:
            engine.shutdown()

        assert len(positions) == 1, f"expected one position, got {positions}"
        assert positions[0].quantity.value == QTY * len(accepted), (
            f"the book holds {positions[0].quantity.value} shares for "
            f"{len(accepted)} accepted fills of {QTY} each; a refused fill "
            f"booked a position (AC-21)"
        )
        assert booked == QTY * len(accepted), (
            f"{booked} shares are booked against {len(accepted)} accepted "
            f"fills; the position book and the accepted set disagree (AC-21)"
        )
        assert len(_logged_fills(store)) == len(accepted), (
            f"{len(_logged_fills(store))} fills are in the durable log but only "
            f"{len(accepted)} were accepted; a refused fill was persisted, and "
            f"recovery would fold it into a position this book never had (AC-2)"
        )
        assert cash >= Decimal("0"), (
            f"cash went negative to {cash}: two submits both passed the check "
            f"and both debited"
        )
        assert cash < self.CAPITAL, "no fill moved any cash at all"


class TestCashLedgerApplyFillDirectly:
    """``CashLedger.apply_fill`` on its own, with no engine around it.

    The inbound door above holds ``CashLedger.transaction()`` across the call,
    so it would still pass if ``apply_fill`` had lost its own lock. Calling
    ``apply_fill`` directly removes every other guard: nothing here wraps,
    replaces or re-implements it, ``CashLedger._lock`` is never touched, and the
    ledger's own critical section is the only thing between the proof and the
    debit. The seam parked here is the ``book`` callback the method is handed.
    """

    #: Fee charged per fill. Any value above zero makes the true per-fill cost
    #: larger than the notional, so a correct ledger accepts at most three and
    #: refuses the rest.
    FEE = Decimal("1")
    COST = NOTIONAL + FEE
    CAPITAL = COST * 3
    AFFORDABLE = int(CAPITAL / COST)  # 3

    def test_the_lock_spans_the_booking_seam_between_proof_and_debit(self) -> None:
        """One submitter at a time books, and the window was staged.

        ``book()`` is the callback the engine passes for exactly this purpose,
        and ``apply_fill`` calls it after the proof and before the debits, under
        the lock it has been holding since the method began. Remove that lock
        and all eight submitters are inside ``book()`` together.
        """
        ledger = CashLedger(initial=self.CAPITAL, allow_negative=False)
        booking = _booked_seam()

        def submit(order_id: str) -> None:
            ledger.apply_fill(
                side=OrderSide.BUY,
                quantity=Quantity(value=QTY),
                price=Price(value=PRICE),
                fee=self.FEE,
                what=f"fill {order_id}",
                book=booking.callback,
            )

        _burst(submit, THREADS)
        report = booking.seam.report()

        _assert_window_was_staged(report)
        _assert_window_was_closed(report)

    def test_a_refused_fill_books_nothing_and_moves_no_money(self) -> None:
        """Cash, fees and the book must all describe the same set of fills.

        Three views of one fact, and a lost lock breaks the last two: fills the
        proof admits go on to book a position and to be charged, fills it
        refuses do neither, and the totals have to agree with the count of fills
        that were actually accepted.
        """
        ledger = CashLedger(initial=self.CAPITAL, allow_negative=False)
        booking = _booked_seam()

        def submit(order_id: str) -> None:
            ledger.apply_fill(
                side=OrderSide.BUY,
                quantity=Quantity(value=QTY),
                price=Price(value=PRICE),
                fee=self.FEE,
                what=f"fill {order_id}",
                book=booking.callback,
            )

        accepted, refused = _burst(submit, THREADS)
        _assert_window_was_staged(booking.seam.report())
        _assert_not_over_subscribed(accepted, refused, self.AFFORDABLE)

        assert len(booking.booked) == len(accepted), (
            f"{len(booking.booked)} fills booked a position but {len(accepted)} "
            f"were accepted; a refused fill got past the proof and was booked "
            f"anyway (AC-21)"
        )
        assert ledger.cash == self.CAPITAL - self.COST * len(accepted), (
            f"cash is {ledger.cash} for {len(accepted)} accepted fills of "
            f"{self.COST} each (capital was {self.CAPITAL}); what moved and "
            f"what was admitted disagree (AC-21)"
        )
        assert ledger.total_fees == self.FEE * len(accepted), (
            f"fees are {ledger.total_fees} for {len(accepted)} accepted fills "
            f"of {self.FEE} each; a fill paid its notional without its fee, or "
            f"a refused fill was charged (AC-21)"
        )
        assert ledger.cash >= Decimal("0"), (
            f"cash went negative to {ledger.cash}: two submits both passed the "
            f"check and both debited"
        )
