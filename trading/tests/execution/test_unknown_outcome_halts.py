"""An unknown broker outcome must halt new admission.

The unknown-outcome path raises ``OrderSubmissionUnknownError`` and withholds
the idempotency reservation, which blocks a retry of the *same* request. It did
not halt the session: a strategy could keep submitting other orders while the
account held a position nobody could see. The kill switch is the control that
stops new exposure, and an uncertain venue state is exactly when it must fire.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.errors import OrderRejectedError, OrderSubmissionUnknownError
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, Price, Quantity

# Import the defining packages, not the tradex_trading.* compatibility shims.
# The shims re-export these by import_module, so the objects are identical, but
# a test that names the authority keeps passing once the shim tree is deleted
# and makes the package dependency explicit.
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import BrokerFillSource
from tradex_execution.recovery import InMemoryEventStore
from tradex_execution.trading_cache import TradingCache
from tradex_reactive.bus import ReactiveBus


def _bus() -> ReactiveBus:
    return ReactiveBus()


def _request() -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
    )


def _engine_with_unknown_outcome() -> ExecutionEngine:
    fill_source = MagicMock()
    fill_source.submission_boundary_crossed = True
    fill_source.submit.side_effect = OSError("connection lost")
    return ExecutionEngine(
        bus=_bus(),
        fill_source=fill_source,
        cache=TradingCache(),
        event_store=InMemoryEventStore(),
        cash=Decimal("100000"),
        require_durable_events=True,
    )


def test_unknown_submission_halts_the_session() -> None:
    """The account may hold an order nobody can see; stop adding exposure."""
    engine = _engine_with_unknown_outcome()
    try:
        with pytest.raises(OrderSubmissionUnknownError):
            engine.submit(_request())

        assert engine.kill_switch is True, (
            "an unknown broker outcome left the session trading"
        )
    finally:
        engine.shutdown()


def test_the_halt_names_the_unknown_outcome_as_its_reason() -> None:
    """The operator must be able to tell this halt from any other."""
    engine = _engine_with_unknown_outcome()
    try:
        with pytest.raises(OrderSubmissionUnknownError):
            engine.submit(_request())

        assert "unknown" in (engine.kill_switch_reason or "").lower(), (
            f"halt reason {engine.kill_switch_reason!r} does not mention the "
            "unknown submission"
        )
    finally:
        engine.shutdown()


def test_a_normal_rejection_does_not_halt_the_session() -> None:
    """The gate must not be stuck shut: an ordinary reject still admits risk."""
    engine = _engine_with_unknown_outcome()
    engine._fill.submission_boundary_crossed = False
    engine._fill.submit.side_effect = OSError("venue said no")
    try:
        engine.submit(_request())

        assert engine.kill_switch is False, (
            "a plain broker rejection was treated as an unknown outcome"
        )
    finally:
        engine.shutdown()


def test_the_unknown_error_still_surfaces_on_the_reactive_path() -> None:
    """Halting must not swallow the signal operators need.

    ``submit()`` is a direct door and propagates the raise by design; the
    reactive path is the one that reports it on the bus, so that is where the
    operator-facing event has to appear.
    """
    errors: list = []
    from tradex_domain.events import ErrorOccurred, PlaceOrderCommand

    bus = _bus()
    bus.of_type(ErrorOccurred).subscribe(errors.append)

    fill_source = MagicMock()
    fill_source.submission_boundary_crossed = True
    fill_source.submit.side_effect = OSError("connection lost")
    engine = ExecutionEngine(
        bus=bus,
        fill_source=fill_source,
        cache=TradingCache(),
        event_store=InMemoryEventStore(),
        cash=Decimal("100000"),
        require_durable_events=True,
    )
    try:
        bus.publish(PlaceOrderCommand(request=_request()))

        surfaced = [
            e for e in errors if isinstance(e.error, OrderSubmissionUnknownError)
        ]
        assert surfaced, "the unknown outcome was not reported on the bus"
        assert engine.kill_switch is True
    finally:
        engine.shutdown()


# ---------------------------------------------------------------------------
# The real fill source, not a mock
#
# The tests above drive a MagicMock whose boundary flag is set by hand, so they
# cannot catch the bug this section pins: BrokerFillSource held
# ``_submission_boundary_crossed`` as instance state that was set on the first
# successful submit and never cleared. Every later failure — including a plain
# adapter-side rejection that provably never left the process — was therefore
# read as an unknown venue outcome, tripping the global kill switch and
# halting the entire book over a bad lot size.
# ---------------------------------------------------------------------------


class _BrokerThatRejectsAfterOneAccept:
    """Accepts the first order, then refuses the rest before any send."""

    def __init__(self) -> None:
        self.calls = 0

    def submit_order(self, request: OrderRequest) -> str:
        self.calls += 1
        if self.calls == 1:
            return "VENUE-1"
        raise OrderRejectedError("quantity is not in the permitted lot size")

    def cancel_order(self, order_id: object) -> None:
        return None


class _BrokerThatFailsAfterSend:
    """The transport dies once the request is already on the wire."""

    def submit_order(self, request: OrderRequest) -> str:
        raise OrderSubmissionUnknownError("transport failed after send")

    def cancel_order(self, order_id: object) -> None:
        return None


def _live_engine(broker: object) -> ExecutionEngine:
    return ExecutionEngine(
        bus=_bus(),
        fill_source=BrokerFillSource(broker),
        cache=TradingCache(),
        event_store=InMemoryEventStore(),
        cash=Decimal("100000"),
        require_durable_events=True,
    )


def _tagged(tag: str) -> OrderRequest:
    request = _request()
    return replace(request, correlation_id=CorrelationId(value=tag))


def test_a_rejection_after_an_earlier_success_does_not_halt() -> None:
    """The regression: one bad lot size must not stop the whole book.

    The first order succeeds, so the flag is latched True. The second is
    refused by the adapter before anything is transmitted. That is a definitive
    rejection: the engine records the order as REJECTED and returns a receipt,
    and the session must keep trading.
    """
    engine = _live_engine(_BrokerThatRejectsAfterOneAccept())
    try:
        engine.submit(_tagged("first"))
        receipt = engine.submit(_tagged("second"))

        assert receipt is not None
        assert receipt.status is OrderStatus.REJECTED, (
            f"expected a definitive REJECTED receipt, got {receipt.status!r}"
        )
        assert engine.kill_switch is False, (
            "a rejection that never reached the venue halted the session"
        )
    finally:
        engine.shutdown()


def test_repeated_rejections_never_latch_the_session_halting() -> None:
    """No accumulation of ordinary rejections may reach the unknown path."""
    engine = _live_engine(_BrokerThatRejectsAfterOneAccept())
    try:
        engine.submit(_tagged("first"))
        for i in range(5):
            receipt = engine.submit(_tagged(f"reject-{i}"))
            assert receipt is not None
            assert receipt.status is OrderStatus.REJECTED

        assert engine.kill_switch is False
    finally:
        engine.shutdown()


def test_a_transport_failure_after_send_still_halts() -> None:
    """The fix must not weaken the genuine case: uncertainty still stops us."""
    engine = _live_engine(_BrokerThatFailsAfterSend())
    try:
        with pytest.raises(OrderSubmissionUnknownError):
            engine.submit(_tagged("uncertain"))

        assert engine.kill_switch is True
        assert "unknown" in (engine.kill_switch_reason or "").lower()
    finally:
        engine.shutdown()
