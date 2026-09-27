"""The live seeded ledger must enforce the same cash floor the backtest does.

``ExecutionEngine.restore_cash`` is the one door a live or paper session gets a
cash ledger through. ``boot()`` constructs the engine with **no** ``cash=`` (so
the ledger does not exist yet), and only then seeds an opening balance read
from broker funds — ``startup.py`` calls ``engine.restore_cash(opening_cash,
re_anchor=True)``, and the recovery fold calls it with the folded balance. That
call *creates* the ledger.

It created it with ``allow_negative=True``, which makes both the affordability
floor and the debit guard in ``CashLedger`` no-ops. So the engine's cash gate —
the one ``_require_cash_for_fill`` drives on both fill doors — was armed in the
backtest (which passes ``cash=`` and therefore takes the constructor's
``allow_negative_cash``) and disarmed in production. The evidence for the fix
looked strong and the production behaviour was unchanged, which is the one
arrangement in which a test suite is worse than no test suite at all.

The consequence is not a half-apply, which is what the backtest used to
produce. It is a *silent full* overdraft: with the floor disarmed, ``require_debit``
passes, the position books, and the notional and fee debits both succeed against
a permissive ledger. A live session sees no error, no refusal, and a cash
balance below zero.

What ``restore_cash`` is, and why that matters here: it is an absolute
**setter**, on every production call site. The cold-boot seed and the recovery
fold are seeds; the corporate-action path in ``BacktestEngine`` is a setter being
*used* as a delta by a caller that reads the snapshot, adds, and writes the sum
back (``_apply_cash_delta``). So the seed is not a debit and the delta is not
this method's argument — the floor belongs in the ledger's policy, and the only
thing to fix is which policy the seeded ledger is born with. The seed itself
must not consult the floor, because an opening balance is not something an
account has to be able to afford.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, OrderType
from tradex_domain.events import ErrorOccurred, OrderFilled
from tradex_domain.execution import Fill, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import (
    CorrelationId,
    OrderId,
    Price,
    Quantity,
)
from tradex_execution.cash_ledger import CashLedger
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fees import FeeCalculator
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_execution.idempotency import MemoryIdempotencyGuard
from tradex_execution.recovery import InMemoryEventStore
from tradex_execution.trading_cache import TradingCache
from tradex_reactive.bus import ReactiveBus

INSTRUMENT = Equity.of("NSE", "RELIANCE")

#: Notional of the standard 10-share fill, so a test can set capital to
#: "exactly the trade" without repeating the multiplication.
NOTIONAL = Decimal("1000")

#: Capital for every refusal control: half the notional, so the fill is
#: unaffordable on the notional alone and never needs the fee to prove it.
CAPITAL = Decimal("500")


def _fill(
    side: OrderSide = OrderSide.BUY,
    qty: str = "10",
    price: str = "100",
    order_id: str = "o1",
    fill_id: str | None = None,
) -> OrderFilled:
    """One ``OrderFilled`` event, as a live fill stream publishes it."""
    return OrderFilled(
        fill=Fill(
            order_id=OrderId(order_id),
            instrument=INSTRUMENT,
            side=side,
            quantity=Quantity(value=Decimal(qty)),
            price=Price(value=Decimal(price)),
            fill_id=fill_id,
        ),
    )


def _live_seeded_engine(
    opening_cash: Decimal = CAPITAL,
    *,
    store: InMemoryEventStore | None = None,
    bus: ReactiveBus | None = None,
) -> ExecutionEngine:
    """An engine shaped exactly as ``boot()`` builds one, then seeded.

    No ``cash=`` — the constructor therefore holds no ledger at all — and the
    seed goes in through ``restore_cash`` with ``re_anchor=True``, which is the
    live/paper cold-boot sequence (``startup.py``). Building it this way rather
    than with a helper is the point: the test has to exercise the door
    production uses, and a helper that passed ``cash=`` would exercise the
    constructor and prove nothing about the seed.
    """
    engine = ExecutionEngine(
        bus=bus if bus is not None else ReactiveBus(),
        fill_source=SimulatedFillSource(),
        cache=TradingCache(),
        event_store=store,
        fee_calculator=FeeCalculator(),
        idempotency_guard=MemoryIdempotencyGuard(),
    )
    assert engine.cash_snapshot().cash == Decimal(0), (
        "the unseeded live engine already holds a ledger; the control below "
        "would then be testing the constructor rather than the seed"
    )
    engine.restore_cash(opening_cash, re_anchor=True)
    return engine


def _backtest_shaped_engine(
    cash: Decimal,
    *,
    allow_negative: bool = False,
    store: InMemoryEventStore | None = None,
) -> ExecutionEngine:
    """An engine built as ``BacktestEngine.run`` builds one: floor armed."""
    return ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=SimulatedFillSource(),
        cache=TradingCache(),
        event_store=store,
        cash=cash,
        allow_negative_cash=allow_negative,
        fee_calculator=FeeCalculator(),
    )


def _logged_fills(store: InMemoryEventStore) -> list[OrderFilled]:
    return [e for e in store.replay("orders") if isinstance(e, OrderFilled)]


def _positions(engine: ExecutionEngine) -> list[Decimal]:
    return [p.quantity.value for p in engine.cache.all_positions()]


class TestALiveSeededSessionRefusesAnUnaffordableFill:
    """Control 1: the live ledger enforces the floor the backtest enforces.

    The numbers below are what the engine actually did with the floor disarmed:
    the position booked, cash landed at ``-500.42``, and ``ErrorOccurred`` was
    never published — so a live session had nothing at all to notice.
    """

    def test_nothing_books_debits_or_logs(self) -> None:
        store = InMemoryEventStore()
        engine = _live_seeded_engine(store=store)
        try:
            with pytest.raises(ValueError, match="insufficient buying power"):
                engine._apply_fill(_fill())  # noqa: SLF001

            assert _positions(engine) == [], (
                "a live seeded session booked a position it cannot pay for"
            )
            assert engine.cash_snapshot().cash == CAPITAL
            assert engine.cash_snapshot().total_fees == Decimal("0")
            assert _logged_fills(store) == [], (
                "a refused fill in the log is folded by recovery into a "
                "position the live book never had"
            )
        finally:
            engine.shutdown()

    def test_a_bus_delivery_reports_the_refusal(self) -> None:
        """The live fill stream arrives on the bus, so the refusal must be seen.

        ``ReactiveBus`` isolates a subscriber's exception, so the raise itself is
        a log line. The engine republishes it as ``ErrorOccurred``; a live boot
        logs and counts that (``startup.py``). Without it the refusal is
        consistent but invisible.
        """
        bus = ReactiveBus()
        store = InMemoryEventStore()
        engine = _live_seeded_engine(store=store, bus=bus)
        errors: list[ErrorOccurred] = []
        bus.of_type(ErrorOccurred).subscribe(errors.append)
        try:
            bus.publish(_fill())

            assert _positions(engine) == [], (
                "the live stream booked a position with no cash behind it"
            )
            assert engine.cash_snapshot().cash == CAPITAL, (
                "a live session overdraws silently: the balance went below zero "
                "with nothing raised, refused or published"
            )
            assert len(errors) == 1
            assert isinstance(errors[0].error, ValueError)
            assert "insufficient buying power" in str(errors[0].error)
            assert _logged_fills(store) == []
        finally:
            engine.shutdown()

    def test_the_submitted_order_is_refused_too(self) -> None:
        """The other door, and the one a paper run actually uses.

        A live session submits orders; it does not hand-roll ``OrderFilled``.
        The synchronous door carries the same shared proof, so a live seeded
        engine must refuse here as well.
        """
        engine = _live_seeded_engine()
        try:
            with pytest.raises(ValueError, match="insufficient buying power"):
                engine.submit(
                    OrderRequest(
                        instrument=INSTRUMENT,
                        side=OrderSide.BUY,
                        quantity=Quantity(NOTIONAL / Decimal("100")),
                        order_type=OrderType.MARKET,
                        price=Price(Decimal("100")),
                        correlation_id=CorrelationId("live-1"),
                    ),
                )
            assert engine.cache.get_position(INSTRUMENT) is None
            assert engine.cash_snapshot().cash == CAPITAL
        finally:
            engine.shutdown()

    def test_the_refused_fill_is_not_a_dedup(self) -> None:
        """A refusal must leave no fingerprint that silences the venue's retry.

        The top-up stands in for new funds arriving. The retry has to be heard.
        """
        engine = _live_seeded_engine()
        try:
            for _ in range(2):
                with pytest.raises(ValueError, match="insufficient buying power"):
                    engine._apply_fill(_fill())  # noqa: SLF001
            assert _positions(engine) == []

            engine.restore_cash(NOTIONAL * 2)
            engine._apply_fill(_fill())  # noqa: SLF001
            assert _positions(engine) == [Decimal("10")]
        finally:
            engine.shutdown()


class TestTheSeedIsASetterNotADebit:
    """Control 2: a legitimate opening balance must not trip its own floor.

    This is the control that catches the classic wrong fix. Implementing the
    seed as a debit — a real ``_debit(opening_cash)``, or seeding into a ledger
    whose floor is checked — takes the balance to ``0 - opening_cash`` and
    refuses every opening balance, including a perfectly ordinary one. It fails
    at boot, loudly, so it is the safe direction to fail; what it must not do is
    pass by accident, which is why every property below is asserted on the same
    seeded ledger rather than inferred.
    """

    def test_the_opening_balance_lands_at_its_face_value(self) -> None:
        engine = _live_seeded_engine()
        try:
            assert engine.cash_snapshot().cash == CAPITAL, (
                "the seed did not set the opening balance to its face value — "
                "a seed implemented as a debit lands at 0 and refuses everything"
            )
        finally:
            engine.shutdown()

    def test_the_floor_is_armed_on_that_same_balance(self) -> None:
        """Face value *and* armed, on one ledger — the pair that separates the two.

        A seed that is a setter leaves 500 in place; the floor is then what
        refuses 500.01. A seed that is a debit leaves 0 and refuses everything,
        including the opening balance. A seed that is a setter on a permissive
        ledger leaves 500 and refuses nothing. Only one of the three passes both
        halves.
        """
        engine = _live_seeded_engine()
        try:
            assert engine.cash_snapshot().cash == CAPITAL

            with pytest.raises(ValueError, match="insufficient buying power"):
                engine._apply_fill(_fill(qty="6", price="100"))  # noqa: SLF001
            assert _positions(engine) == [], "a 600 fill booked against 500"
            assert engine.cash_snapshot().cash == CAPITAL

            # And a fill the account can pay for is not refused: the floor is a
            # floor, not a gate that is stuck shut.
            engine._apply_fill(_fill(qty="4", price="100"))  # noqa: SLF001
            assert _positions(engine) == [Decimal("4")]
        finally:
            engine.shutdown()

    def test_a_zero_opening_balance_is_a_valid_seed(self) -> None:
        """Zero is a figure a broker can legitimately report.

        So it must be accepted as a balance, and then refuse a buy — which is
        the fail-closed "cash unknown" shape the live gate already relies on.
        """
        engine = _live_seeded_engine(Decimal(0))
        try:
            assert engine.cash_snapshot().cash == Decimal(0)
            with pytest.raises(ValueError, match="insufficient buying power"):
                engine._apply_fill(_fill())  # noqa: SLF001
        finally:
            engine.shutdown()

    def test_the_seed_preserves_an_existing_ledger_policy(self) -> None:
        """Re-seeding a held ledger is a setter, so the floor is untouched.

        ``restore_cash`` on an engine that already holds a ledger has always
        gone through ``CashLedger.restore``, which assigns and never consults the
        floor. A top-up or a fold must keep working exactly as it did.
        """
        engine = _backtest_shaped_engine(CAPITAL)
        try:
            engine.restore_cash(NOTIONAL * 2)
            assert engine.cash_snapshot().cash == NOTIONAL * 2
            with pytest.raises(ValueError, match="insufficient buying power"):
                engine._apply_fill(_fill(qty="30"))  # noqa: SLF001
        finally:
            engine.shutdown()


class TestTheBacktestBehaviourIsUnchanged:
    """Control 3: lane c2's result, and the no-gate mode, both still hold.

    The backtest builds its engine with ``cash=`` and
    ``allow_negative_cash=(risk_manager is None)``, so it never reaches the
    seeded branch. Both arms are asserted here anyway: "the backtest still
    refuses" and "the historical no-gate mode still overdraws" are two different
    claims, and a fix to the live path that armed the floor everywhere would
    fail the second one.
    """

    def test_the_backtest_shape_still_refuses(self) -> None:
        engine = _backtest_shaped_engine(CAPITAL)
        try:
            with pytest.raises(ValueError, match="insufficient buying power"):
                engine._apply_fill(_fill())  # noqa: SLF001
            assert _positions(engine) == []
            assert engine.cash_snapshot().cash == CAPITAL
        finally:
            engine.shutdown()

    def test_the_no_gate_mode_still_overdraws(self) -> None:
        """``allow_negative_cash=True`` keeps unlimited buying power, by design.

        That is the backtest-with-no-risk-manager mode, and turning it into a
        refusal would be a different lane's decision to make.
        """
        engine = _backtest_shaped_engine(CAPITAL, allow_negative=True)
        try:
            engine._apply_fill(_fill())  # noqa: SLF001
            assert _positions(engine) == [Decimal("10")]
            assert engine.cash_snapshot().cash < Decimal(0)
        finally:
            engine.shutdown()


class TestAWholesaleRestoreIsStillWholesale:
    """A fold or a corporate-action delta adopts a figure; it is not a spend.

    ``restore_cash`` is an absolute setter on every production call site. The
    recovery fold adopts a balance computed by folding the event log, and
    ``BacktestEngine``'s ``_apply_cash_delta`` reads the snapshot, adds, and
    writes the sum back. Neither is a debit, so neither may be refused by the
    floor — including the negative ones. Refusing them would refuse every
    legitimate opening balance and every split restatement.

    That leaves a real hole, stated here rather than hidden: a corporate action
    can drive a run's balance below zero with nothing raised. Closing it means
    a *delta* door on the engine with its own refusal, which is ``CashLedger``'s
    to own (lane 2h) and not this lane's — the setter cannot distinguish a
    corporate action from a fold, and guessing would break the seed.
    """

    def test_a_negative_delta_still_applies_and_keeps_the_fee_total(self) -> None:
        engine = _backtest_shaped_engine(NOTIONAL * 2)
        try:
            engine._apply_fill(_fill())  # noqa: SLF001
            fees = engine.cash_snapshot().total_fees
            assert fees > Decimal(0), "no fee means this control proves nothing"

            # Exactly ``BacktestEngine._apply_cash_delta``'s read-modify-write.
            snapshot = engine.cash_snapshot()
            engine.restore_cash(snapshot.cash - NOTIONAL, snapshot.total_fees)

            assert engine.cash_snapshot().cash < Decimal(0), (
                "a negative corporate-action delta was refused or clamped — the "
                "setter must adopt the figure it is given"
            )
            assert engine.cash_snapshot().total_fees == fees, (
                "the fee total must survive a cash restore, or recovery reports "
                "a different fee bill than the run charged"
            )
        finally:
            engine.shutdown()

    def test_a_recovered_fold_that_is_overdrawn_is_adopted_then_guarded(
        self,
    ) -> None:
        """The fail-closed direction is spending, not reading a balance.

        A log written by a permissive ledger can fold to a negative balance —
        that is precisely the state the pre-fix live path left behind, and a
        restarted process reading such a log is how one meets this lane's fix.
        The restore has to accept it: the fold is authoritative, and unknown is
        not zero. The floor then refuses the spending an overdrawn account
        cannot do.
        """
        engine = _live_seeded_engine(Decimal("-500"))
        try:
            assert engine.cash_snapshot().cash == Decimal("-500"), (
                "the restore refused a balance the log is authoritative for"
            )

            with pytest.raises(ValueError, match="insufficient buying power"):
                engine._apply_fill(_fill())  # noqa: SLF001
            assert _positions(engine) == []
        finally:
            engine.shutdown()

    def test_an_overdrawn_account_can_still_not_sell(self) -> None:
        """**Inherited behaviour, pinned, not endorsed.** See the report.

        ``_require_cash_for_fill`` holds a SELL to its fee against the balance
        as it stands, before that fill's own credit lands (lane c2's formula,
        reused verbatim from 1ab's on the synchronous door). A negative balance
        can therefore never cover a fee, so **every** SELL on an overdrawn
        account is refused — not only the empty-account case c2 tested, which a
        positive balance cannot reach either.

        That is the sharp edge of arming the floor on the live path, and it is
        an argument for lane 2h settling the formula in ``CashLedger``'s single
        ``apply_fill`` rather than an argument against this lane: the states
        that reach it are overdrawn ones, and an account that cannot sell is
        worse than one that is merely overdrawn. The fix is to charge the fee
        against the balance *including* this fill's own credit, which
        ``cash_ledger.py`` owns and this lane does not.
        """
        engine = _live_seeded_engine(Decimal("-500"))
        try:
            with pytest.raises(ValueError, match="insufficient buying power"):
                engine._apply_fill(_fill(side=OrderSide.SELL, fill_id="s1"))  # noqa: SLF001
            assert _positions(engine) == []
            assert engine.cash_snapshot().cash == Decimal("-500")
        finally:
            engine.shutdown()


class TestNegativeControlTheSeededFloorIsLoadBearing:
    """Remove the floor and the live shape is back to overdrawing silently.

    Both arms run in one test on one input, so the control cannot rot into
    passing vacuously. The floor is disarmed at ``CashLedger``'s constructor —
    the enforcement point itself, not the engine's wiring of it — so the control
    does not depend on how the fix is factored. Arm B's state *is* the defect,
    pinned: a full silent overdraft, with no refusal and no error published.
    """

    def test_armed_the_fill_is_refused_disarmed_it_is_applied_at_a_loss(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        armed_bus = ReactiveBus()
        armed = _live_seeded_engine(store=InMemoryEventStore(), bus=armed_bus)
        armed_errors: list[ErrorOccurred] = []
        armed_bus.of_type(ErrorOccurred).subscribe(armed_errors.append)
        try:
            armed_bus.publish(_fill())
            assert _positions(armed) == []
            assert armed.cash_snapshot().cash == CAPITAL
            assert len(armed_errors) == 1
        finally:
            armed.shutdown()

        # Disarm: a ledger constructed the way the live seed used to build one.
        real_init = CashLedger.__init__

        def permissive_init(
            self: CashLedger,
            initial: Decimal | float | str = Decimal(100000),
            allow_negative: bool = False,
        ) -> None:
            real_init(self, initial, True)

        monkeypatch.setattr(CashLedger, "__init__", permissive_init)

        disarmed_bus = ReactiveBus()
        disarmed = _live_seeded_engine(
            store=InMemoryEventStore(), bus=disarmed_bus,
        )
        disarmed_errors: list[ErrorOccurred] = []
        disarmed_bus.of_type(ErrorOccurred).subscribe(disarmed_errors.append)
        try:
            disarmed_bus.publish(_fill())

            assert _positions(disarmed) == [Decimal("10")], (
                "with the floor removed the live seeded engine books the "
                "unaffordable position anyway"
            )
            assert disarmed.cash_snapshot().cash == CAPITAL - NOTIONAL - (
                engine_fee()
            ), "the balance did not land where the pre-fix live path landed it"
            assert disarmed.cash_snapshot().cash < Decimal(0), (
                "a live session overdraws with no refusal and no error"
            )
            assert disarmed_errors == [], (
                "the pre-fix live path published nothing, so the overdraft was "
                "invisible to the session"
            )
        finally:
            disarmed.shutdown()


def engine_fee() -> Decimal:
    """The fee the engine charges for the standard 10-share BUY, not a literal."""
    return FeeCalculator().calculate(
        Fill(
            order_id=OrderId("o1"),
            instrument=INSTRUMENT,
            side=OrderSide.BUY,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("100")),
        ),
    ).amount
