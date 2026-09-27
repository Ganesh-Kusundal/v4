"""Inbound-fill cash atomicity — the recorded-``Fill`` door (AC-2).

``ExecutionEngine._apply_fill`` handles a ``Fill`` arriving on the event bus:
the durable order store's replay and mirror path, a broker's async order
stream, and the only door a backtest fed a recorded ``Fill`` tape uses
(``BacktestEngine.run`` publishes each tape entry as ``OrderFilled``).

It used to book the position and *then* debit the cash ledger, with nothing in
front of either. So a fill the account cannot afford booked a position, the
debit raised inside ``CashLedger._debit``, ``ReactiveBus._isolate`` logged and
dropped it — and the position stayed booked with no cash behind it. The live
book and the recovered book disagreed permanently, and the only evidence was a
log line.

The mechanism is the one lane 1ab built for the synchronous door: the engine
owned ``CashLedger`` and its ``require_debit``, which proves affordability
*before* anything is booked, so the booking and the two debits that follow
cannot half-apply. The two doors had drifted apart; they now share one
``_require_cash_for_fill``.

Where the proof sits differs between the doors, deliberately. On the
synchronous door the durable append happens first, because a pipeline fill is
the venue's execution of an order the client asked for. Here it cannot: a
refused fill that reached the log would be folded by recovery into a position
the live book never had, and a refused fill whose dedup fingerprint was
committed would have its own retry silently swallowed. So on this door the
proof runs ahead of both the write and the fingerprint, and it is the first
thing that can raise. ``require_debit`` reads and never mutates, so "persist
before mutating" still holds for the first mutation, which is the booking.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import NamedTuple

import pytest
from tradex_domain.enums import OrderSide
from tradex_domain.events import ErrorOccurred, OrderFilled
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

#: Notional of the standard 10-share fill, so a test can set capital to
#: "exactly the trade" without repeating the multiplication.
NOTIONAL = Decimal("1000")


def _fill(
    side: OrderSide = OrderSide.BUY,
    qty: str = "10",
    price: str = "100",
    order_id: str = "o1",
    fill_id: str | None = None,
) -> OrderFilled:
    """One ``OrderFilled`` event. No recorded fee, so the engine charges one."""
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


def _engine(
    cash: str,
    *,
    store: InMemoryEventStore | None = None,
    bus: ReactiveBus | None = None,
) -> ExecutionEngine:
    """An engine with its ledger armed: an opening balance, floor enforced."""
    return ExecutionEngine(
        bus=bus if bus is not None else ReactiveBus(),
        fill_source=SimulatedFillSource(),
        cache=TradingCache(),
        event_store=store,
        cash=Decimal(cash),
        allow_negative_cash=False,
        fee_calculator=FeeCalculator(),
    )


def _logged_fills(store: InMemoryEventStore) -> list[OrderFilled]:
    return [e for e in store.replay("orders") if isinstance(e, OrderFilled)]


class _State(NamedTuple):
    """The two books plus the log, so idempotence is compared in full."""

    positions: tuple[tuple[str, str, str], ...]
    cash: Decimal
    total_fees: Decimal
    logged: int

    @classmethod
    def of(cls, engine: ExecutionEngine, store: InMemoryEventStore) -> _State:
        return cls(
            positions=tuple(
                (
                    str(p.instrument),
                    str(p.quantity.value),
                    str(p.avg_price.value),
                )
                for p in engine.cache.all_positions()
            ),
            cash=engine.cash_snapshot().cash,
            total_fees=engine.cash_snapshot().total_fees,
            logged=len(_logged_fills(store)),
        )


class TestUnaffordableInboundFillIsRefusedWhole:
    """Control 1: an unaffordable fill must leave the book unchanged."""

    def test_nothing_is_booked_debited_or_logged(self) -> None:
        store = InMemoryEventStore()
        engine = _engine("500", store=store)
        try:
            with pytest.raises(ValueError, match="insufficient buying power"):
                engine._apply_fill(_fill())  # noqa: SLF001
            assert engine.cache.all_positions() == [], (
                "the position was booked by a fill the account cannot pay for"
            )
            assert engine.cash_snapshot().cash == Decimal("500")
            assert engine.cash_snapshot().total_fees == Decimal("0")
            assert _logged_fills(store) == [], (
                "a refused fill in the log is folded by recovery into a "
                "position the live book never had"
            )
        finally:
            engine.shutdown()

    def test_the_order_is_not_advanced(self) -> None:
        """No order may read FILLED for a fill the book refused."""
        store = InMemoryEventStore()
        engine = _engine("500", store=store)
        try:
            with pytest.raises(ValueError):
                engine._apply_fill(_fill())  # noqa: SLF001
            assert engine.get_order(OrderId("o1")) is None
        finally:
            engine.shutdown()

    def test_the_fee_is_part_of_the_requirement(self) -> None:
        """Capital of exactly the notional is not enough: the fee is too.

        The last order an account can afford is the one its own fee makes
        unaffordable, so a check on the notional alone would pass a fill the
        ledger's own debit then refuses — the half-apply this lane closes.
        """
        fee = FeeCalculator().calculate(
            Fill(
                order_id=OrderId("o1"),
                instrument=INSTRUMENT,
                side=OrderSide.BUY,
                quantity=Quantity(value=Decimal("10")),
                price=Price(value=Decimal("100")),
            ),
        ).amount
        assert fee > Decimal("0"), "no fee means this control proves nothing"

        store = InMemoryEventStore()
        engine = _engine(str(NOTIONAL), store=store)
        try:
            with pytest.raises(ValueError, match="insufficient buying power"):
                engine._apply_fill(_fill())  # noqa: SLF001
            assert engine.cache.all_positions() == []
        finally:
            engine.shutdown()

        # The control's sibling: one fee more, the same fill is affordable.
        engine = _engine(str(NOTIONAL + fee), store=InMemoryEventStore())
        try:
            engine._apply_fill(_fill())  # noqa: SLF001
            assert engine.cache.all_positions()[0].quantity.value == Decimal("10")
            assert engine.cash_snapshot().cash == Decimal("0")
        finally:
            engine.shutdown()

    def test_a_sell_is_held_to_its_fee_alone(self) -> None:
        """A SELL credits the notional, so the formula holds it to the fee.

        The check reads the balance as it stands, before this fill's own credit
        lands, so a sell out of an empty account is refused on a fee its credit
        would have covered. That is the synchronous door's formula, reused
        verbatim rather than re-decided per door — the two doors differing is
        how this defect existed in the first place. It fails closed, and it
        turns away only the case where the account holds no cash at all. See
        the report: lane 2h owns ``CashLedger`` and is where it is settled.
        """
        fee = FeeCalculator().calculate(
            Fill(
                order_id=OrderId("o1"),
                instrument=INSTRUMENT,
                side=OrderSide.SELL,
                quantity=Quantity(value=Decimal("10")),
                price=Price(value=Decimal("100")),
            ),
        ).amount
        assert fee > Decimal("0"), "no fee means this control proves nothing"

        engine = _engine("0", store=InMemoryEventStore())
        try:
            with pytest.raises(ValueError, match="insufficient buying power"):
                engine._apply_fill(_fill(side=OrderSide.SELL))  # noqa: SLF001
            assert engine.cache.all_positions() == []
        finally:
            engine.shutdown()

        # One fee in the account, the same sell applies and credits in full.
        engine = _engine(str(fee), store=InMemoryEventStore())
        try:
            engine._apply_fill(_fill(side=OrderSide.SELL))  # noqa: SLF001
            assert engine.cash_snapshot().cash == NOTIONAL
        finally:
            engine.shutdown()

    def test_the_refused_fill_leaves_no_trace_that_silences_its_own_retry(
        self,
    ) -> None:
        """A refusal is not a dedup: the venue's retry must still be heard.

        This is what commits the proof ahead of ``_record_applied_fill``. A
        fingerprint recorded for a fill the book never applied turns the retry
        into a silent no-op, and the fill is lost for good with no error.
        """
        store = InMemoryEventStore()
        engine = _engine("500", store=store)
        try:
            for _ in range(2):
                with pytest.raises(ValueError, match="insufficient buying power"):
                    engine._apply_fill(_fill())  # noqa: SLF001
            assert _logged_fills(store) == []
            assert engine.cache.all_positions() == []
        finally:
            engine.shutdown()

        # Top the account up: the same fill now applies, exactly once.
        engine.restore_cash(NOTIONAL * 2)
        engine._apply_fill(_fill())  # noqa: SLF001
        assert engine.cache.all_positions()[0].quantity.value == Decimal("10")


class TestTheRaiseSurvivesTheBus:
    """The bus isolates subscriber errors, so the engine republishes them.

    ``ReactiveBus._isolate`` logs and drops an exception raised by a
    subscriber — that is the swallow the defect report quotes. A raise that
    stops at the log leaves a backtest's numbers quietly missing a fill, so
    the engine puts the failure on the bus as ``ErrorOccurred``, which
    ``BacktestEngine.run`` treats as run-ending and the live boot logs and
    counts.
    """

    def test_a_bus_delivery_books_nothing_and_reports_the_error(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        bus = ReactiveBus()
        store = InMemoryEventStore()
        engine = _engine("500", store=store, bus=bus)
        errors: list[ErrorOccurred] = []
        bus.of_type(ErrorOccurred).subscribe(errors.append)
        try:
            with caplog.at_level(logging.ERROR, logger="tradex_reactive.bus"):
                bus.publish(_fill())

            assert engine.cache.all_positions() == [], (
                "the bus delivery booked a position with no cash behind it"
            )
            assert engine.cash_snapshot().cash == Decimal("500")
            assert len(errors) == 1
            assert isinstance(errors[0].error, ValueError)
            assert "insufficient buying power" in str(errors[0].error)
            # The isolation log line is still there, so the bus keeps telling.
            assert any(
                "insufficient buying power" in r.getMessage()
                and r.name == "tradex_reactive.bus"
                for r in caplog.records
            )
        finally:
            engine.shutdown()

    def test_an_affordable_fill_reports_no_error(self) -> None:
        bus = ReactiveBus()
        engine = _engine("5000", store=InMemoryEventStore(), bus=bus)
        errors: list[ErrorOccurred] = []
        bus.of_type(ErrorOccurred).subscribe(errors.append)
        try:
            bus.publish(_fill())
            assert errors == []
            assert engine.cache.all_positions()[0].quantity.value == Decimal("10")
        finally:
            engine.shutdown()


class TestNegativeControlThePreCheckIsLoadBearing:
    """Control 2: remove the proof and the half-apply is back.

    Both arms run in one test, on one input, so the control cannot rot into
    passing vacuously. ``CashLedger.require_debit`` is neutered rather than
    ``_require_cash_for_fill``, so the control does not depend on how this fix
    is factored: the affordability floor itself is what stops the booking.
    """

    def test_with_the_floor_nothing_books_without_it_the_position_does(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arm A: the floor is in place. The fill is refused whole.
        armed = _engine("500", store=InMemoryEventStore())
        try:
            with pytest.raises(ValueError, match="insufficient buying power"):
                armed._apply_fill(_fill())  # noqa: SLF001
            assert armed.cache.all_positions() == []
            assert armed.cash_snapshot().cash == Decimal("500")
        finally:
            armed.shutdown()

        # Arm B: the floor removed. Same input, same raise — but it has moved
        # to the debit that runs AFTER the booking, which is the defect: a
        # position with no cash behind it, and a log entry saying it applied.
        monkeypatch.setattr(
            CashLedger, "require_debit", lambda self, amount, what: None,
        )
        store = InMemoryEventStore()
        engine = _engine("500", store=store)
        try:
            with pytest.raises(ValueError, match="insufficient buying power"):
                engine._apply_fill(_fill())  # noqa: SLF001

            assert engine.cache.all_positions()[0].quantity.value == Decimal("10"), (
                "with the floor removed the position books anyway — this is the "
                "half-applied fill the fix prevents"
            )
            assert engine.cash_snapshot().cash == Decimal("500")
            assert len(_logged_fills(store)) == 1
        finally:
            engine.shutdown()


class TestTheSameFillStreamTwiceIsOneStream:
    """Control 3: this door is fed by a durable log, so delivery repeats.

    Every pass builds **fresh** ``OrderFilled`` and ``Fill`` objects, the way a
    log replay does. Identity is not available to a re-delivery, so only the
    fingerprint dedup can make this safe — and it has to survive the pre-check
    landing in front of it.
    """

    @staticmethod
    def _stream() -> list[OrderFilled]:
        """BUY 10 @ 100, then SELL 4 @ 120 — two orders, a partial close."""
        return [
            _fill(order_id="o1", fill_id="trade-1"),
            _fill(
                side=OrderSide.SELL,
                qty="4",
                price="120",
                order_id="o2",
                fill_id="trade-2",
            ),
        ]

    @classmethod
    def _replay(cls, repeats: int) -> _State:
        bus = ReactiveBus()
        store = InMemoryEventStore()
        engine = _engine("5000", store=store, bus=bus)
        try:
            for _ in range(repeats):
                for event in cls._stream():
                    bus.publish(event)
            return _State.of(engine, store)
        finally:
            engine.shutdown()

    def test_one_pass_equals_two_equals_three(self) -> None:
        once = self._replay(1)
        assert len(once.positions) == 1, "the stream itself did not apply"
        assert once.positions[0][1] == "6", "10 bought then 4 sold is 6"
        assert once.logged == 2
        assert once.cash < Decimal("5000"), "a BUY stream must move cash"

        assert self._replay(2) == once, "a re-delivered stream double booked"
        assert self._replay(3) == once, "a third delivery was not idempotent"

    def test_the_repeat_does_not_double_debit_or_double_book(self) -> None:
        once = self._replay(1)
        twice = self._replay(2)
        assert twice.cash == once.cash, "cash moved once per pass"
        assert twice.total_fees == once.total_fees, "fees were charged twice"
        assert len(twice.positions) == 1
        assert twice.positions[0][1] == "6", "10 bought then 4 sold is 6, not 2"
        assert twice.logged == once.logged == 2, "the log grew on re-delivery"

    def test_a_refused_stream_delivered_twice_is_also_idempotent(self) -> None:
        """Idempotence must hold on the failure path too, not only the happy one.

        Two deliveries, two errors, and neither one books or logs anything: a
        refusal that is not a no-op would be a divergence of its own.
        """
        bus = ReactiveBus()
        store = InMemoryEventStore()
        engine = _engine("500", store=store, bus=bus)
        errors: list[ErrorOccurred] = []
        bus.of_type(ErrorOccurred).subscribe(errors.append)
        try:
            for _ in range(2):
                bus.publish(_fill())
            assert len(errors) == 2
            assert engine.cache.all_positions() == []
            assert engine.cash_snapshot().cash == Decimal("500")
            assert _logged_fills(store) == []
        finally:
            engine.shutdown()
