"""A broker timeout on order submission must never file a definitive rejection.

This is the money-safety invariant behind commit 6191b9e's follow-up work, and
it is the highest-severity regression guard in this directory.

The hazard is concrete: if a submission that already left the process is filed
as ``REJECTED``, the correlation id is released, the kill switch stays untripped,
and a client that retries its own "rejected" order places a **second live order**
at the venue under the same idempotency key. The local book then shows one
rejection while the account holds two fills.

Why a bare ``TimeoutError`` is the interesting input
----------------------------------------------------
``TimeoutError`` is a subclass of ``OSError`` (see ``issubclass`` assertion
below -- pinned deliberately, because the whole guarantee rests on it). A
timeout means *no response arrived*, which is not the same as *the venue
refused*: the request crossed the wire and the venue may have accepted it.

The production path that turns that into a safe outcome is four links, and this
module exercises all four together rather than mocking the middle of it:

1. ``ProviderHttpClient.submit_mutation`` catches ``OSError`` (the
   ``_UNCERTAIN_SEND_ERRORS`` tuple) and re-raises the typed
   ``OrderSubmissionUnknownError``, marking the correlation key unresolved.
2. ``BrokerFillSource._dispatch`` re-raises that typed error with the
   ``submission_boundary_crossed`` latch left set.
3. ``ExecutionEngine`` sees a venue-reaching source whose latch is set, so it
   transitions the order to ``OrderStatus.UNKNOWN`` and persists an
   ``UnknownSubmission`` plus the ``OrderPlaced`` that carries it.
4. The engine trips the kill switch and re-raises, so the caller cannot mistake
   this for a rejection and retry it as one.

What is asserted, and what deliberately is not
---------------------------------------------
Asserted: the engine raises ``OrderSubmissionUnknownError`` rather than
returning a ``REJECTED`` receipt; the kill switch is active (a retry is refused
rather than admitted); an ``UnknownSubmission`` is durably written to the event
store; and the order was filed as ``UNKNOWN``.

Not asserted: that the order's *final* cached status is ``UNKNOWN``. It is not.
``KillSwitch.trip`` cancels every non-terminal order, and ``UNKNOWN`` is
deliberately non-terminal (``kill_switch.TERMINAL_STATUSES`` excludes it
precisely because the order may be live at the venue), so the order settles to
``CANCELLED`` after the halt. Pinning ``CANCELLED`` here would be pinning a
*consequence* of the halt and would break the moment the halt policy changes;
the guard that matters is that the order passed through ``UNKNOWN`` and that no
``REJECTED`` receipt was ever returned. ``_statuses_seen`` records the full
lifecycle so the intermediate ``UNKNOWN`` is checked, not just the endpoint.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from tradex_brokers.common.provider_client import ProviderHttpClient
from tradex_brokers.common.transport import HttpTransport
from tradex_domain.enums import OrderSide, OrderStatus, OrderType
from tradex_domain.errors import OrderSubmissionUnknownError
from tradex_domain.events import OrderPlaced, UnknownSubmission
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, Price, Quantity
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import BrokerFillSource
from tradex_execution.sqlite_event_store import SQLiteEventStore
from tradex_reactive.bus import ReactiveBus

_INSTRUMENT = Equity.of("NSE", "RELIANCE")
_CORRELATION = "timeout-corr-1"


# ---------------------------------------------------------------------------
# Fakes: the broker boundary, not the thing under test
# ---------------------------------------------------------------------------


class _TimeoutPipeline:
    """A send pipeline whose ``send`` times out *after* the request left.

    This is the real production failure: the transport is waiting on a response
    that will never come. Standing in for ``ResiliencePipeline`` keeps the test
    hermetic -- no socket is opened and no wall-clock timeout is waited on.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def send(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append((method, url))
        raise TimeoutError("read timed out after the request was sent")


class _TimeoutBroker:
    """Broker facade shaped like the real adapters' ``submit_order``.

    ``dhan/_orders.py`` and ``upstox/_orders.py`` both do exactly this: hand
    the placement to ``submit_mutation`` and forward its result. Mirroring that
    one call keeps the test honest -- the conversion under test is the broker
    layer's, not something this fake invents.
    """

    def __init__(self) -> None:
        self.pipeline = _TimeoutPipeline()
        self._http = ProviderHttpClient(
            transport=HttpTransport(base_url="https://api.example.test"),
            pipeline=self.pipeline,
        )

    def submit_order(self, request: OrderRequest) -> Any:
        correlation = (
            str(request.correlation_id.value)
            if request.correlation_id is not None
            else None
        )
        return self._http.submit_mutation(
            "POST",
            "/orders",
            operation="place_order",
            correlation_id=correlation,
            json={"symbol": "RELIANCE"},
        )

    def cancel_order(self, order_id: Any) -> Any:
        # Reached only by the kill switch's halt, which must be able to reach
        # the venue; the timed-out placement leaves nothing to cancel there.
        return None


def _request(correlation: str = _CORRELATION) -> OrderRequest:
    return OrderRequest(
        instrument=_INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("2500")),
        correlation_id=CorrelationId(correlation),
    )


def _engine(broker: _TimeoutBroker, store: SQLiteEventStore) -> ExecutionEngine:
    return ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=BrokerFillSource(broker),
        idempotency_guard=None,
        event_store=store,
    )


def _statuses_seen(store: SQLiteEventStore) -> list[OrderStatus]:
    """Every order status the engine durably filed, in order."""
    return [
        event.order.status
        for event in store.replay("*")
        if isinstance(event, OrderPlaced)
    ]


# ---------------------------------------------------------------------------
# The money-safety invariant
# ---------------------------------------------------------------------------


def test_timeout_error_is_a_subclass_of_oserror() -> None:
    """The conversion below is only sound while this holds.

    ``submit_mutation`` discriminates uncertain sends with
    ``_UNCERTAIN_SEND_ERRORS = (OSError,)``. If a timeout were ever re-parented
    outside ``OSError``, the broker layer would stop converting it and the
    engine would file a definitive REJECTED -- exactly the regression these
    tests exist to catch, one layer below where it would otherwise be visible.
    """
    assert issubclass(TimeoutError, OSError)


def test_broker_submit_converts_a_wire_timeout_to_unknown_submission() -> None:
    """Link 1: a post-send timeout must not escape as a plain TimeoutError.

    This is the seam where the venue's silence is turned into an explicit
    "the outcome is unknown" signal, and the correlation key is marked
    unresolved so a blind resubmission is refused.
    """
    broker = _TimeoutBroker()

    with pytest.raises(OrderSubmissionUnknownError):
        broker.submit_order(_request())

    assert broker.pipeline.calls == [("POST", "https://api.example.test/orders")], (
        "the submission must actually have been attempted before the timeout; "
        f"pipeline saw {broker.pipeline.calls!r}"
    )


def test_engine_files_unknown_and_halts_instead_of_rejecting(tmp_path) -> None:
    """Links 2-4: a timed-out live submission becomes UNKNOWN, never REJECTED.

    The whole point. A client reading a REJECTED here would retry and place a
    second live order; a client reading this must reconcile instead.
    """
    store = SQLiteEventStore(str(tmp_path / "events.db"))
    engine = _engine(_TimeoutBroker(), store)

    try:
        with pytest.raises(OrderSubmissionUnknownError) as caught:
            engine.submit(_request())

        # The caller is told the outcome is unknown -- never handed a receipt
        # that reads as a refusal it may safely retry.
        assert "unknown" in str(caught.value).lower()
        assert engine.kill_switch is True, (
            "an account may now hold an order nothing local can see; admission "
            "must halt until it is reconciled, or other strategies keep adding "
            "exposure to a book of unknown state"
        )

        filed = _statuses_seen(store)
        assert OrderStatus.UNKNOWN in filed, (
            f"the order must be filed as UNKNOWN; statuses recorded were {filed!r}"
        )
        assert OrderStatus.REJECTED not in filed, (
            "a submission that crossed the venue boundary was filed as REJECTED; "
            "a client retry of its own rejection places a second live order"
        )
    finally:
        store.close()


def test_unknown_submission_is_durable_before_the_raise(tmp_path) -> None:
    """The crash-safety half: the record must outlive the process.

    ``_append(UnknownSubmission(...))`` happens *before* the raise, so a crash
    between the venue accepting the order and this process noticing leaves a
    durable breadcrumb. Without it the order exists only at the broker, and
    reconciliation reads it as unexplained drift.
    """
    store = SQLiteEventStore(str(tmp_path / "events.db"))
    engine = _engine(_TimeoutBroker(), store)

    try:
        with pytest.raises(OrderSubmissionUnknownError):
            engine.submit(_request())

        recorded = [e for e in store.replay("*") if isinstance(e, UnknownSubmission)]
        assert recorded, (
            "no UnknownSubmission reached the event store; a crash here would "
            "leave an order live at the venue that nothing local accounts for"
        )
        assert recorded[0].request.correlation_id is not None
        assert str(recorded[0].request.correlation_id.value) == _CORRELATION
    finally:
        store.close()


def test_a_retry_of_a_timed_out_correlation_is_not_admitted(tmp_path) -> None:
    """The consequence, asserted directly: no second live order, ever.

    Without the kill switch the second submit would be a fresh submission and
    would reach the venue again. With it, the retry is refused at intake.
    """
    store = SQLiteEventStore(str(tmp_path / "events.db"))
    broker = _TimeoutBroker()
    engine = _engine(broker, store)

    try:
        with pytest.raises(OrderSubmissionUnknownError):
            engine.submit(_request())

        attempts_before = len(broker.pipeline.calls)

        # The retry is refused at intake by the kill switch. A sync submit
        # reports that refusal as a receipt rather than raising, so the receipt
        # -- and the absence of a second broker call -- is the evidence.
        retry_receipt = engine.submit(_request(correlation=_CORRELATION))

        assert retry_receipt.message == "kill_switch_active", (
            "the retry must be refused by the kill switch, not sent; got "
            f"status={retry_receipt.status} message={retry_receipt.message!r}"
        )
        assert retry_receipt.status is OrderStatus.REJECTED, (
            "the refused retry is a kill-switch rejection, which is safe: it "
            "never crossed the venue"
        )
        assert len(broker.pipeline.calls) == attempts_before, (
            "the retry reached the broker; a second live order is exactly the "
            "failure these tests exist to prevent"
        )
    finally:
        store.close()


def test_a_definitive_broker_rejection_is_still_a_rejection(tmp_path) -> None:
    """The guard has a boundary, and this pins the other side of it.

    ``_dispatch`` clears the boundary latch for a failure the adapter raises
    *before* anything is transmitted (a bad lot size, a capability gate). Those
    must stay rejections: tripping the global kill switch and telling the
    operator the venue may hold an order it never received would halt the whole
    book over a local typo.
    """
    from tradex_domain.errors import OrderRejectedError

    class _LocalValidationBroker:
        def submit_order(self, request: OrderRequest) -> Any:
            raise OrderRejectedError("lot size 7 is not a valid multiple")

    store = SQLiteEventStore(str(tmp_path / "events.db"))
    engine = _engine(_LocalValidationBroker(), store)  # type: ignore[arg-type]

    try:
        receipt = engine.submit(_request())

        assert receipt is not None
        assert receipt.status is OrderStatus.REJECTED, (
            "a failure proven local is a rejection; filing it UNKNOWN would halt "
            "the book for an order that provably never left the process"
        )
        assert engine.kill_switch is False, (
            "a local validation failure must not trip the global kill switch"
        )
        assert not [e for e in store.replay("*") if isinstance(e, UnknownSubmission)]
    finally:
        store.close()
