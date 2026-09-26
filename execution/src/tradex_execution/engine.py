"""Reactive execution engine — the v4 order spine.

Wires the reactive pipeline: idempotency → risk → fill → OMS, all as
RxPY operators on the ReactiveBus.  This is the core of v4 — the
reactive execution pipeline.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from tradex_domain.enums import (
    OrderStatus,
    OrderType,
    TimeInForce,
)
from tradex_domain.errors import OrderRejectedError
from tradex_domain.events import (
    BrokerOrderAcknowledged,
    BrokerOrderRequested,
    CancelRequested,
    CashAccountInitialized,
    DomainEvent,
    ErrorOccurred,
    OrderCancelled,
    OrderFilled,
    OrderModified,
    OrderPlaced,
    OrderRejected,
    PlaceOrderCommand,
    ReconciliationResult,
    RiskDecision,
    UnknownSubmission,
)
from tradex_domain.execution import (
    BracketOrderRequest,
    Fill,
    Order,
    OrderReceipt,
    OrderRequest,
    Position,
)
from tradex_domain.protocols import Clock
from tradex_domain.value_objects import CorrelationId, Money, OrderId
from tradex_reactive.bus import ReactiveBus
from tradex_reactive.thread_safe_bus import ThreadSafeReactiveBus

from tradex_execution.cash_ledger import CashLedger, CashSnapshot
from tradex_execution.fees import FeeCalculator
from tradex_execution.fill_sources import FillSource
from tradex_execution.idempotency import (
    FillDedup,
    IdempotencyDuplicate,  # noqa: F401 — re-exported for callers of engine
    IdempotencyGuard,
    IdempotencyInflight,
    IdempotencyKeyReuseMismatch,  # noqa: F401 — re-exported
    MemoryIdempotencyGuard,  # noqa: F401 — re-exported for runtime/startup
)
from tradex_execution.kill_switch import TERMINAL_STATUSES, KillSwitch
from tradex_execution.order_manager import OrderManager
from tradex_execution.position_manager import PositionManager
from tradex_execution.reconciliation import DriftItem, ReconciliationEngine
from tradex_execution.risk import (
    RiskCheckResult,  # noqa: F401 — re-exported for callers of engine
    RiskManager,
)
from tradex_execution.sqlite_store import (  # noqa: F401 — re-exported
    InMemoryOrderStore,
    OrderStore,
)
from tradex_execution.trading_cache import TradingCache

if TYPE_CHECKING:
    from tradex_observability.metrics import MetricsRegistry

    from tradex_execution.recovery import EventStore


@runtime_checkable
class FeedReady(Protocol):
    """Minimal feed gate used by the engine — avoids importing runtime."""

    @property
    def ready(self) -> bool: ...


log = logging.getLogger(__name__)

#: Settled orders cannot be modified. Shares the kill switch's definition so
#: the two cannot disagree about what "done" means; UNKNOWN is excluded because
#: the venue may still hold it, so it is still ours to act on.
_TERMINAL_STATUSES = TERMINAL_STATUSES


def _is_bracket_order(order: Order) -> bool:
    """True when *order* is a bracket (super) order in the OMS.

    Bracket submissions carry protective legs (``stop_loss_price`` +
    ``target_price``), and the fill sources persist those legs onto the
    created ``Order`` — the legs are the OMS-side marker that later
    lifecycle mutations must use the venue's super-order endpoints.
    """
    return order.stop_loss_price is not None and order.target_price is not None


def _canonical_decimal(value: Decimal | None) -> str:
    """Return one stable textual representation for a Decimal value."""
    if value is None:
        return ""
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _request_fingerprint(
    request: OrderRequest,
    *,
    operation: str = "submit",
    target: str | None = None,
) -> str:
    """Canonical, operation-scoped fingerprint for an order mutation.

    The idempotency key identifies a *mutation*, not merely a correlation
    string.  Binding the operation and target prevents a submit key from
    being replayed as a modify/cancel key, while canonical Decimal rendering
    makes economically equivalent values (``10``, ``10.0``) hash alike.
    """
    instrument_id = getattr(request.instrument, "instrument_id", request.instrument)
    reference = request.reference_timestamp
    return "|".join([
        operation,
        target or "",
        str(instrument_id),
        request.side.value,
        request.order_type.value,
        _canonical_decimal(request.quantity.value),
        _canonical_decimal(request.price.value if request.price is not None else None),
        _canonical_decimal(
            request.trigger_price.value if request.trigger_price is not None else None
        ),
        getattr(request.time_in_force, "value", request.time_in_force),
        getattr(request.product_type, "value", request.product_type),
        str(request.disclosed_quantity),
        str(request.market_protection),
        request.tag or "",
        reference.isoformat() if reference is not None else "",
        _canonical_decimal(
            request.stop_loss_price.value
            if getattr(request, "stop_loss_price", None) is not None else None
        ),
        _canonical_decimal(
            request.target_price.value
            if getattr(request, "target_price", None) is not None else None
        ),
        _canonical_decimal(
            request.trailing_jump.value
            if getattr(request, "trailing_jump", None) is not None else None
        ),
    ])


def _cancel_fingerprint(order_id: OrderId) -> str:
    """Canonical fingerprint for a cancel mutation."""
    return f"cancel|{order_id.value}"

class ExecutionEngine:
    """Single order spine as reactive pipeline.

    idempotency → risk → fill → OMS, all as Observable operators.
    This is the core of v4 — the reactive execution pipeline.

    **Dual entry points (R5 architecture decision):**

    Orders can enter the pipeline through two doors:

    1. **Imperative** — ``engine.submit(request)`` for direct submission.
    2. **CQRS command** — publish ``PlaceOrderCommand`` on the bus for
       strategy-driven submission.

    Both paths share the same idempotency guard, so a duplicate request
    is caught regardless of entry point. However, strategies should pick
    **one** pattern and not mix them — mixing imperative and command
    submission on the same guard can create subtle ordering races.
    """

    def __init__(
        self,
        bus: ReactiveBus | ThreadSafeReactiveBus,
        fill_source: FillSource,
        risk_manager: RiskManager | None = None,
        idempotency_guard: IdempotencyGuard | None = None,
        cache: TradingCache | None = None,
        metrics: MetricsRegistry | None = None,
        fee_calculator: FeeCalculator | None = None,
        cash: Decimal | None = None,
        allow_negative_cash: bool = False,
        applied_fills_max: int = 50_000,
        clock: Clock | None = None,
        feed_supervisor: FeedReady | None = None,
        event_store: EventStore | None = None,
        require_risk: bool = False,
        require_feed: bool = False,
        require_durable_events: bool = False,
    ) -> None:
        """
        fee_calculator:
            When provided, every applied fill's fees are deducted from the
            position's realized P&L — making reactive paper/live net P&L
            consistent with BacktestEngine's net cash accounting (HIGH-6b).
        applied_fills_max:
            H3: hard cap on the size of the applied-fills LRU dedup set.
            Defaults to 50_000 (sufficient for a full trading day at the
            design rate). Tight-memory deployments and tests can lower it.
            Each LRU eviction increments ``bus.applied_fills.evicted``.
        """
        self._bus = bus
        self._fill = fill_source
        self._risk = risk_manager
        self._guard = idempotency_guard
        self._cache = cache or TradingCache()
        self._metrics = metrics
        self._clock = clock
        self._feed_supervisor = feed_supervisor
        self._event_store = event_store
        self._require_risk = require_risk
        self._require_feed = require_feed
        self._require_durable_events = require_durable_events
        self._guard_lock = threading.Lock()
        self._fee_calculator = fee_calculator
        self._cash_ledger = (
            CashLedger(initial=cash, allow_negative=allow_negative_cash)
            if cash is not None
            else None
        )
        self._order_manager = OrderManager(self._cache)
        self._position_manager = PositionManager(self._cache)
        self._kill_switch = KillSwitch(metrics=metrics, risk=risk_manager, cache=self._cache)
        self._reconciler = ReconciliationEngine()
        self._brokerage_accrued: dict[str, Decimal] = {}
        self._brokerage_lock = threading.Lock()  # ponytail: unbounded, LRU 50k if needed
        #: Bounded LRU dedup for applied fill events — re-published broker
        #: fills are skipped, distinct partial fills are each applied in full.
        #: Eviction is observable via the ``bus.applied_fills.evicted`` counter.
        self._fill_dedup = FillDedup(
            max_size=applied_fills_max,
            on_evict=lambda: (
                self._metrics.counter("bus.applied_fills.evicted").inc()
                if self._metrics is not None else None
            ),
        )
        #: M2: side-table mapping each order_id to the CorrelationId reserved
        #: for it in the pipeline. Populated when ``check_and_reserve`` returns
        #: ``None`` (cid is fresh); consulted by ``cancel()`` so the cid is
        #: released when the order is cancelled (the pipeline normally records
        #: the result for FILLED orders, but a cancellation never reaches that
        #: path). Cleared from the table once the cid is released so the
        #: mapping never leaks between orders.
        self._cid_for_order: dict[OrderId, CorrelationId] = {}
        self._setup_pipeline()
        # The opening balance is the root of the cash fold: without it in the
        # log a restarted process cannot rebuild the ledger at all. An engine
        # constructed with an explicit ``cash=`` already knows its opening
        # balance, so it anchors here; one whose funds arrive later in boot
        # anchors from ``restore_cash``.
        self._cash_anchor_appended = False
        if self._cash_ledger is not None:
            # An explicit ``cash=`` IS a new opening balance, so it anchors.
            self.restore_cash(self._cash_ledger.cash, re_anchor=True)

    def _setup_pipeline(self) -> None:
        """Wire the reactive order pipeline using RxPY operators.

        Subscribes to OrderRequest messages on the bus and processes them
        through the pipeline: idempotency → risk → fill → OMS update → publish.
        Also subscribes to PlaceOrderCommand for CQRS-style order submission.
        """

        # Kill-switch intake rejections emit OrderRejected inside _run_pipeline;
        # do not filter here or the reactive path would drop silently.
        self._pipeline_disposable = self._bus.of_type(OrderRequest).subscribe(
            on_next=self._process_request,
            # Unexpected pipeline failures surface as ErrorOccurred. Per-request
            # failures already emit well-formed OrderRejected events inside
            # _process_request, so we never fabricate an Order here.
            on_error=lambda e: self._bus.publish(ErrorOccurred(error=e)),
        )

        # CQRS command subscription — strategies publish PlaceOrderCommand
        # to the bus instead of calling broker adapters directly.
        self._command_disposable = self._bus.of_type(PlaceOrderCommand).subscribe(
            on_next=lambda cmd: self._process_request(cmd.request),
            on_error=lambda e: self._bus.publish(ErrorOccurred(error=e)),
        )

        # Inbound live-fill bridge — broker order streams (or any publisher)
        # publish OrderFilled on the bus; the engine applies the fill to the
        # OMS idempotently. This is what makes live fills reach the position
        # manager (BrokerFillSource returns ACK-with-no-fill synchronously).
        self._fill_disposable = self._bus.of_type(OrderFilled).subscribe(
            on_next=self._apply_fill,
            on_error=lambda e: self._bus.publish(ErrorOccurred(error=e)),
        )

    # ------------------------------------------------------------------
    # Durable event store hook (Wave C1)
    # ------------------------------------------------------------------

    def _append(self, event: DomainEvent) -> bool:
        """Append *event* to the durable store when one is configured.

        Returns True when the event is durably stored (or no store is
        configured) and False when the write failed. A caller that is about to
        mutate state MUST check this: persisting after the mutation would let a
        failed write leave the book and the log permanently disagreeing.

        When ``require_durable_events`` is False (default), failures are logged
        and swallowed so a store write never crashes the pipeline. When True
        (live fail-closed), a failed append trips the kill switch and publishes
        ``ErrorOccurred`` — subsequent submits reject; an in-flight fill that
        already crossed the broker boundary is not rolled back.

        ``ponytail:`` ceiling: each call acquires the SQLite WAL lock; for
        high-throughput paths this adds ~10 µs per append. Upgrade path: async
        batch writer.
        """
        if self._event_store is None:
            return True
        try:
            self._event_store.append(event)
        except Exception as exc:
            log.exception(
                "EventStore.append failed for %s; pipeline continues",
                type(event).__name__,
            )
            if self._require_durable_events:
                if not self._kill_switch.active:
                    self._kill_switch.trip(
                        f"EventStore.append failed: {exc}",
                        cancel=self.cancel,
                    )
                self._bus.publish(ErrorOccurred(error=exc))
            return False
        return True

    def _reject_intake(
        self,
        request: OrderRequest,
        reason: str,
        *,
        sync: bool,
        metric_suffix: str | None = None,
    ) -> OrderReceipt | None:
        """Publish a terminal intake rejection (feed/risk/kill-switch gates)."""
        if metric_suffix is not None and self._metrics is not None:
            self._metrics.counter(
                f"orders.rejection_reason.{metric_suffix}",
            ).inc()
        order = self._make_order(request, OrderStatus.REJECTED)
        self._order_manager.on_order_created(order)
        rejected_event = OrderRejected(order=order, reason=reason)
        self._bus.publish(rejected_event)
        self._append(rejected_event)
        if self._metrics is not None:
            self._metrics.counter("orders.rejected").inc()
        if sync:
            return OrderReceipt(
                order_id=order.order_id,
                status=OrderStatus.REJECTED,
                message=reason,
            )
        return None

    def shutdown(self) -> None:
        """Gracefully shut down the execution engine."""
        log.info("ExecutionEngine shutting down...")
        self._kill_switch.set()
        if hasattr(self, "_pipeline_disposable") and self._pipeline_disposable is not None:
            try:
                self._pipeline_disposable.dispose()
            except Exception as exc:
                log.error("Error disposing pipeline: %s", exc)
        if hasattr(self, "_command_disposable") and self._command_disposable is not None:
            try:
                self._command_disposable.dispose()
            except Exception as exc:
                log.error("Error disposing command subscription: %s", exc)
        if hasattr(self, "_fill_disposable") and self._fill_disposable is not None:
            try:
                self._fill_disposable.dispose()
            except Exception as exc:
                log.error("Error disposing fill subscription: %s", exc)
        # Close an injected durable guard (e.g. SQLiteIdempotencyGuard) so its
        # connection is released on shutdown — not leaked for the process life.
        guard_close = getattr(self._guard, "close", None)
        if callable(guard_close):
            try:
                guard_close()
            except Exception as exc:  # pragma: no cover
                log.error("Error closing idempotency guard: %s", exc)
        log.info("ExecutionEngine shutdown complete")

    def __enter__(self) -> ExecutionEngine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()

    def _process_request(self, request: OrderRequest) -> None:
        """Process a single order request through the pipeline."""
        t0 = time.perf_counter()
        try:
            self._process_request_impl(request)
        except Exception as exc:
            # ReactiveBus swallows subscriber exceptions; surface them explicitly.
            self._bus.publish(ErrorOccurred(error=exc))
        finally:
            if self._metrics is not None:
                self._metrics.histogram("orders.process_latency_seconds").observe(
                    time.perf_counter() - t0,
                )

    def _run_pipeline(
        self, request: OrderRequest, *, sync: bool
    ) -> OrderReceipt | None:
        """Single idempotency→risk→fill→OMS sequence shared by the reactive
        and synchronous submit paths.

        ``sync`` controls the return shape: the reactive path returns ``None``
        (fire-and-forget on the bus); the synchronous path returns an
        ``OrderReceipt``. The kill-switch is checked **before** reserving the
        idempotency correlation id (residual review Task 3) so a trip between
        the two never leaks a permanently-reserved cid.
        """
        log.info(
            "Processing order request for %s (cid=%s, sync=%s)",
            request.instrument, request.correlation_id, sync,
        )

        # 0. Kill switch (cheap — must precede any reservation)
        if self._kill_switch.active:
            return self._reject_intake(
                request,
                "kill_switch_active",
                sync=sync,
                metric_suffix="kill_switch_active",
            )

        if self._require_feed and self._feed_supervisor is None:
            return self._reject_intake(
                request,
                "FEED_UNBOUND",
                sync=sync,
                metric_suffix="feed_unbound",
            )

        if self._feed_supervisor is not None and not self._feed_supervisor.ready:
            return self._reject_intake(
                request,
                "feed_not_ready",
                sync=sync,
                metric_suffix="feed_not_ready",
            )

        if self._require_risk and self._risk is None:
            return self._reject_intake(
                request,
                "RISK_UNBOUND",
                sync=sync,
                metric_suffix="risk_unbound",
            )

        # 1. Idempotency check
        cid = request.correlation_id
        reserved_cid: CorrelationId | None = None
        if self._guard is not None and cid is not None:
            try:
                dup = self._guard.check_and_reserve(
                    cid, request_hash=_request_fingerprint(
                        request, operation="submit", target=None,
                    ),
                )
            except IdempotencyInflight:
                # The original request still owns the key. Answer
                # deterministically instead of leaking a 500 (N5): the
                # reservation is NOT released here.
                if sync:
                    return OrderReceipt(
                        order_id=OrderId(value="pending"),
                        status=OrderStatus.PENDING,
                        message="idempotency_in_flight",
                    )
                return None
            if dup is not None:
                # v3 parity: silently replay — original events were already published
                log.info("Idempotency replay for correlation %s", cid)
                if self._metrics is not None:
                    self._metrics.counter("orders.idempotency_replay").inc()
                return dup.result if sync else None
            # M2: reservation succeeded — record the cid in the side-table
            # so a later cancel() can release it. The order_id is unknown
            # yet (it comes from the fill source below); record after the
            # fill step where the order is created.
            reserved_cid = cid

        # 2. Risk check
        # Pre-bound before the venue call: a broker that raises leaves nothing
        # assigned, and the unknown-outcome path still has to record an order.
        order: Order | None = None
        fill: Fill | None = None
        risk_rejected = False
        if self._risk is not None and not self._risk.check(request):
            log.warning("Risk check failed for order")
            risk_rejected = True
            order = self._make_order(request, OrderStatus.REJECTED)
            self._order_manager.on_order_created(order)
            _rejected_event = OrderRejected(order=order, reason="risk_check_failed")
            self._bus.publish(_rejected_event)
            # Wave C1: persist risk verdict + rejection to durable store
            self._append(RiskDecision(request=request, approved=False, reason="risk_check_failed"))
            self._append(_rejected_event)
            if self._metrics is not None:
                self._metrics.counter("orders.rejected").inc()
                self._metrics.counter("risk.rejected").inc()
            # M2: release the reservation on risk-reject so the cid is            # immediately reusable. Risk rejection is a terminal state for
            # the request — the order never reaches the cancel path and
            # would otherwise leak the cid forever.
            if reserved_cid is not None and self._guard is not None:
                with self._guard_lock:
                    self._guard.release(reserved_cid)
        elif self._risk is not None:
            # Risk passed — persist the approval. A failed write means the log
            # cannot account for this order, so it must not reach the venue.
            if not self._append(RiskDecision(request=request, approved=True)):
                log.error(
                    "Risk approval not durable for %s; order not sent to venue",
                    request.correlation_id,
                )
                return self._no_receipt(sync, "risk_decision_not_durable")

        if risk_rejected:
            if order is None:
                order = self._make_order(request, OrderStatus.REJECTED)
            return (
                OrderReceipt(
                    order_id=order.order_id,
                    status=OrderStatus.REJECTED,
                    message="risk_check_failed",
                )
                if sync
                else None
            )

        # 3. Fill — Wave C1: persist broker dispatch intent before the call.
        # This write is the only durable record that the order may be live at
        # the venue. If it fails, sending anyway means a crash immediately
        # after leaves an order at the broker that nothing local accounts for,
        # so the pipeline stops here instead.
        if not self._append(BrokerOrderRequested(request=request)):
            log.error(
                "Dispatch intent not durable for %s; order not sent to venue",
                request.correlation_id,
            )
            return self._no_receipt(sync, "dispatch_intent_not_durable")
        # Pre-bound: a venue that raises leaves nothing assigned, and the
        # unknown-outcome path still has to record an order for the OMS.
        try:
            order, fill = self._fill.submit(request)
        except Exception as exc:
            # The authoritative signal for "the venue may hold this order" is
            # the typed exception the broker layer raises, not a local flag.
            # `ProviderClient.submit_mutation` (common/provider_client.py:291)
            # already distinguishes a transport failure *after send* and a 5xx
            # — genuinely uncertain — from every definitive failure, and raises
            # OrderSubmissionUnknownError for the former.
            #
            # The old `submission_boundary_crossed` latch could not make that
            # distinction: it was set before the adapter was entered, so any
            # adapter-side validation (a bad lot size, a capability gate, a
            # disabled live-order gate) counted as "crossed", and the engine
            # tripped the global kill switch on a rejection that had provably
            # never left the process — halting the whole book and telling the
            # operator the venue may hold an order it never received.
            #
            # `submission_boundary_crossed` is still honoured for third-party
            # fill sources that only expose the flag, but a typed
            # OrderSubmissionUnknownError is now sufficient on its own, so a
            # source that never sets the flag is no longer misfiled.
            from tradex_domain.errors import OrderSubmissionUnknownError

            boundary_crossed = getattr(
                self._fill, "submission_boundary_crossed", False,
            ) or isinstance(exc, OrderSubmissionUnknownError)
            if boundary_crossed:
                # C3: unknown outcome — submitted but result uncertain
                if self._metrics is not None:
                    self._metrics.counter("orders.unknown_outcome_total").inc()
                # Wave C1: persist unknown submission before raising. The OMS
                # must hold the order too: reconciliation compares the local
                # book with the venue's, and an order that exists only at the
                # broker reads as unexplained drift.
                self._append(UnknownSubmission(request=request, detail=str(exc)))
                if order is None:
                    order = self._make_order(request, OrderStatus.NEW)
                unknown_order = order.transition_to(OrderStatus.UNKNOWN)
                self._order_manager.on_order_created(unknown_order)
                self._append(OrderPlaced(order=unknown_order))
                order = unknown_order
                # The account may now hold an order nobody can see. Withholding
                # the reservation blocks a retry of THIS request, but other
                # strategies would keep adding exposure to a book whose true
                # state is unknown — so halt admission until it is reconciled.
                self._kill_switch.trip(
                    "unknown_broker_outcome: the venue may have accepted an "
                    f"order this process cannot account for ({exc})",
                    cancel=self.cancel,
                )
                raise OrderSubmissionUnknownError(
                    f"Order submission failed after crossing broker boundary: {exc}"
                ) from exc
            if reserved_cid is not None and self._guard is not None:
                with self._guard_lock:
                    self._guard.release(reserved_cid)
            reserved_cid = None
            order = self._make_order(request, OrderStatus.REJECTED)
            self._order_manager.on_order_created(order)
            _fill_err_event = OrderRejected(order=order, reason=str(exc))
            self._bus.publish(_fill_err_event)
            # Wave C1: persist fill-source rejection
            self._append(_fill_err_event)
            if self._metrics is not None:
                self._metrics.counter("orders.rejected").inc()
            return (
                OrderReceipt(
                    order_id=order.order_id,
                    status=OrderStatus.REJECTED,
                    message=str(exc),
                )
                if sync
                else None
            )

        # 4. OMS update
        self._order_manager.on_order_created(order)
        if reserved_cid is not None and self._guard is not None:
            with self._guard_lock:
                self._guard.record_result(reserved_cid, order.order_id)
        # M2: stamp the order_id → cid mapping so cancel() can release
        # the reservation. The order has just been created; from here
        # forward the cid is owned by this order. record_result() above
        # removes the entry from the reserved set but does not clear the
        # side-table — cancel() needs the cid even after record_result.
        if reserved_cid is not None:
            self._cid_for_order[order.order_id] = reserved_cid
        _placed_event = OrderPlaced(order=order)
        self._bus.publish(_placed_event)
        # Wave C1: persist order placed + broker ack
        self._append(_placed_event)
        self._append(BrokerOrderAcknowledged(order=order))

        if fill is not None:
            # Guard: skip position update if fill source owns projection
            # (PaperBroker projects positions itself). Idempotent against
            # _apply_fill: the order is FILLED before OrderFilled is published.
            charged_fee = Decimal(0)
            if not getattr(self._fill, "position_projection_owned", False):
                charged_fee = self._fee_for_fill(fill)
            _filled_event = OrderFilled(fill=fill, fee_amount=charged_fee)
            # Persist BEFORE mutating. The event is authoritative and the cache
            # is a projection: if the write fails after the book has moved, the
            # two disagree permanently and a restart rebuilds a book missing a
            # fill the venue already executed. A failed write therefore aborts
            # the projection update — the kill switch has already halted new
            # admission, and the venue's fill is recovered by reconciliation.
            if not self._append(_filled_event):
                # The kill switch has already halted admission; the venue's
                # fill is recovered by reconciliation, so the book stays
                # consistent with the log. The order still gets a receipt so a
                # synchronous caller is not left without one.
                log.error(
                    "Fill not durable; leaving the book unchanged for %s",
                    fill.order_id,
                )
            else:
                if not getattr(self._fill, "position_projection_owned", False):
                    self._position_manager.on_fill(fill)
                    self._apply_cash_fill(fill)
                    self._apply_fee(fill, charged_fee)
                self._order_manager.on_order_filled(order, fill)
                # G3: record the fingerprint in the applied-fills set BEFORE
                # publishing so that any re-publish from the live-fill bridge
                # (which subscribes to OrderFilled) is short-circuited by
                # the dedup check in _apply_fill.
                self._record_applied_fill(fill)
                self._bus.publish(_filled_event)
            # Record idempotency result for replay
            if self._guard is not None and cid is not None:
                self._guard.record_result(cid, order.order_id)
            log.info(
                "Order filled: %s qty=%s price=%s (cid=%s)",
                order.order_id, fill.quantity, fill.price, cid,
            )
            if self._metrics is not None:
                self._metrics.counter("orders.submitted").inc()
                self._metrics.counter("orders.filled").inc()
        else:
            # An ACK-only broker submission is still a completed idempotent
            # request. Persist the provider order id before returning so a
            # client retry cannot submit a second live order while waiting for
            # the asynchronous fill stream.
            if reserved_cid is not None and self._guard is not None:
                with self._guard_lock:
                    self._guard.record_result(reserved_cid, order.order_id)
            if self._metrics is not None:
                self._metrics.counter("orders.submitted").inc()

        if not sync:
            return None
        return OrderReceipt(
            order_id=order.order_id,
            status=order.status,
            message="submitted",
        )

    def _process_request_impl(self, request: OrderRequest) -> None:
        """Reactive pipeline entry — fire-and-forget (returns nothing)."""
        self._run_pipeline(request, sync=False)

    def restore_cash(
        self,
        cash: Decimal,
        total_fees: Decimal = Decimal(0),
        *,
        re_anchor: bool = False,
    ) -> None:
        """Seed the cash ledger from an opening balance or a recovery fold.

        A restarted process starts with an empty ledger, so without this it
        reports ``0`` and the fail-closed cash gate denies every BUY against a
        balance the process actually holds.

        *re_anchor* appends a fresh ``CashAccountInitialized``. That is only
        correct for a genuinely NEW opening balance (a cold boot reading broker
        funds). A restart restoring a fold must leave it False: the anchor is
        already in the log, and appending the folded balance again would both
        grow the log and make the next fold re-apply the fills on top of a
        balance that already includes them.
        """
        if self._cash_ledger is None:
            self._cash_ledger = CashLedger(initial=cash, allow_negative=True)
        else:
            self._cash_ledger.restore(cash, total_fees)
        if re_anchor:
            if not self._cash_anchor_appended:
                # Only a *successful* write proves the log carries an anchor. If
                # this fails, the flag stays False and a later boot knows the
                # account is still unanchored instead of trusting a phantom one.
                self._cash_anchor_appended = self._append(
                    CashAccountInitialized(amount=cash),
                )
                return
        # A restore proves an anchor already exists in the log, so record that
        # even when this call did not write one. Without it a restarted process
        # would still look like a cold boot and anchor a second time.
        self._cash_anchor_appended = True

    @property
    def cash_anchor_missing(self) -> bool:
        """True when no opening anchor has been written for this ledger.

        Startup uses it to tell a cold boot (a new opening balance must be
        anchored) from a restart (the log already has one).
        """
        return not self._cash_anchor_appended

    def cash_snapshot(self) -> CashSnapshot:
        """Return reactive cash state, or a disabled zero snapshot."""
        if self._cash_ledger is None:
            return CashSnapshot(cash=Decimal(0), total_fees=Decimal(0))
        return self._cash_ledger.snapshot()

    def _apply_cash_fill(self, fill: Fill) -> None:
        if self._cash_ledger is not None:
            self._cash_ledger.on_fill(fill.side, fill.quantity, fill.price)

    def _compute_fee(self, fill: Fill) -> Decimal:
        """Compute *fill*'s fee and advance the capped-brokerage accrual.

        Separate from application so the charged amount is known *before* any
        state moves and can be persisted with the event. The ₹20 per-order cap
        accrues across a partial-fill sequence, so replay cannot re-derive this
        from a single fill (H2).
        """
        if self._fee_calculator is None:
            return Decimal(0)
        oid = fill.order_id.value if hasattr(fill.order_id, "value") else str(fill.order_id)
        with self._brokerage_lock:
            accrued = self._brokerage_accrued.get(oid, Decimal(0))
            fee, self._brokerage_accrued[oid] = self._fee_calculator.calculate_capped(
                fill, accrued,
            )
        return fee.amount

    def _fee_for_fill(self, fill: Fill) -> Decimal:
        """The fee to charge for *fill*: recorded if replaying, else computed.

        A replay source carries the fee actually charged on the original run.
        Using it keeps replayed history equal to the run it replays; recomputing
        from today's rate table would silently rewrite past costs.
        """
        recorded = getattr(self._fill, "recorded_fee_for", None)
        if recorded is not None:
            fee = recorded(fill)
            if fee is not None:
                return fee
        return self._compute_fee(fill)

    def _apply_fee(self, fill: Fill, charged: Decimal | None = None) -> None:
        """Deduct *fill*'s fee from the position's realized P&L and cash.

        *charged* is the already-computed amount from :meth:`_compute_fee`;
        omit it to compute and apply in one step.

        Failures propagate loudly — a silently-swallowed fee bug is exactly the
        accounting divergence the parity work exists to prevent.
        """
        if charged is None:
            charged = self._compute_fee(fill)
        if charged <= 0:
            return
        fee = Money(amount=charged)
        self._position_manager.on_fee(fill, fee)
        if self._cash_ledger is not None:
            self._cash_ledger.on_fee(fee)

    def _no_receipt(self, sync: bool, message: str) -> OrderReceipt | None:
        """Terminate the pipeline before it can move money.

        Used when a durable write that must precede the venue call failed. The
        kill switch is already tripped by ``_append``; this stops *this* order
        rather than only subsequent ones.
        """
        return (
            OrderReceipt(
                order_id=OrderId(value=str(uuid.uuid4())),
                status=OrderStatus.REJECTED,
                message=message,
            )
            if sync
            else None
        )

    def _make_order(self, request: OrderRequest, status: OrderStatus) -> Order:
        """Create an Order from a request with the given status."""
        return Order(
            order_id=OrderId(value=str(uuid.uuid4())),
            instrument=request.instrument,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            price=request.price,
            time_in_force=request.time_in_force,
            status=status,
            correlation_id=request.correlation_id,
            trigger_price=request.trigger_price,
            product_type=request.product_type,
            tag=request.tag,
            # Preserve protective legs so bracket identity survives on
            # rejected/OMS records (see ``_is_bracket_order``).
            target_price=request.target_price,
            stop_loss_price=request.stop_loss_price,
            trailing_jump=request.trailing_jump,
        )

    def _record_applied_fill(self, fill: Any) -> bool:
        """Insert the fill's fingerprint into the dedup LRU.

        Delegates to FillDedup. Returns True if duplicate (skip apply).
        """
        return self._fill_dedup.check_and_record(fill)

    def _seen_applied_fill(self, fill: Any) -> bool:
        """True when this exact fill was already applied (without recording)."""
        return self._fill_dedup.seen(fill)

    def _apply_fill(self, event: OrderFilled) -> None:
        """Apply an inbound OrderFilled to the OMS (live-fill bridge).

        Each ``OrderFilled`` event is one fill occurrence (broker order
        stream → bus). A re-published occurrence is skipped via the
        applied-fill fingerprint set, so duplicates never double-apply while
        distinct partial fills each land in full. The synchronous pipeline
        path marks the order FILLED before publishing, so its own event is a
        no-op here.

        Dedup key: when the venue provides a ``fill.fill_id`` (exchange trade
        id), it uniquely identifies the occurrence — two genuine equal-lot
        partial fills with distinct fill ids are both applied. Without a
        fill id the fingerprint falls back to (order, side, quantity, price),
        so equal-lot partials without venue ids remain indistinguishable from
        a re-publish.
        """
        fill = event.fill
        # Membership is checked WITHOUT recording: the fingerprint is committed
        # only after the durable write succeeds, so a failed write leaves the
        # venue's identical retry free to be applied rather than looking like a
        # duplicate forever.
        if self._seen_applied_fill(fill):
            # Re-published fingerprint (same fill_id, or same
            # (order, side, qty, price) when no fill_id). Skip
            # silently — this is the contract that protects the
            # synchronous pipeline path from double-apply via
            # the live-fill bridge, and protects the bridge from
            # its own re-publishes.
            return

        existing = self._cache.get_order(fill.order_id.value)
        if existing is not None and existing.status == OrderStatus.REJECTED:
            # REJECTED stays rejected — do not position-update.
            return
        # Honour the fee recorded on the event. Replaying a historical fill
        # under today's rate table would charge a different number than the one
        # the venue actually applied.
        charged_fee = event.fee_amount
        if charged_fee is None:
            charged_fee = (
                Decimal(0)
                if getattr(self._fill, "position_projection_owned", False)
                else self._compute_fee(fill)
            )
        owns_projection = getattr(self._fill, "position_projection_owned", False)
        # Persist BEFORE mutating — same invariant as the sync fill path.
        if not self._append(OrderFilled(fill=fill, fee_amount=charged_fee)):
            log.error(
                "Inbound fill not durable; leaving the book unchanged for %s",
                fill.order_id,
            )
            return
        # The write landed, so this fill is now applied and must dedup.
        self._record_applied_fill(fill)
        if not owns_projection:
            self._position_manager.on_fill(fill)
            self._apply_cash_fill(fill)
            self._apply_fee(fill, charged_fee)
        if existing is not None:
            # Existing order (any non-rejected status). G3: a
            # *distinct* fill (different fingerprint) is a real
            # partial — apply it. A re-published same-fingerprint
            # fill was already short-circuited above.
            self._order_manager.on_order_filled(existing, fill)
        else:
            # Unknown order — record a minimal FILLED order so reconciliation
            # sees the fill (e.g. fills for orders placed outside this engine).
            self._cache.update_order(
                Order(
                    order_id=fill.order_id,
                    instrument=fill.instrument,
                    side=fill.side,
                    order_type=OrderType.MARKET,
                    quantity=fill.quantity,
                    price=fill.price,
                    time_in_force=TimeInForce.DAY,
                    status=OrderStatus.FILLED,
                    filled_quantity=fill.quantity,
                    correlation_id=getattr(fill, "correlation_id", None),
                    tag=getattr(fill, "tag", None),
                )
            )
        log.info(
            "Inbound fill applied: %s qty=%s price=%s (engine fill bridge)",
            fill.order_id, fill.quantity, fill.price,
        )

    def submit(self, request: OrderRequest) -> OrderReceipt:
        """Synchronous submit — bridges to reactive pipeline.

        Creates the order, runs the fill source, updates OMS, and
        publishes events. Returns an OrderReceipt immediately.

        This imperative door and the reactive ``PlaceOrderCommand``
        subscription share the identical pipeline; choose per call-site
        (imperative for services/API, command for strategies).
        """
        submit_start = time.perf_counter()
        try:
            return self._submit_impl(request)
        finally:
            if self._metrics is not None:
                self._metrics.histogram("orders.submit_latency_seconds").observe(
                    time.perf_counter() - submit_start,
                )

    def _submit_impl(self, request: OrderRequest) -> OrderReceipt:
        """Synchronous submit logic — delegates to the shared pipeline."""
        receipt = self._run_pipeline(request, sync=True)
        if receipt is None:
            raise RuntimeError("sync submit produced no receipt")  # pragma: no cover
        return receipt

    def trip_kill_switch(self, reason: str = "") -> list[str]:
        """Halt new submissions and cancel every open order.

        Delegates to :class:`KillSwitch`, which owns the halt logic; the cancel
        callable is the engine's own ``cancel`` so venue dispatch stays here.
        """
        return self._kill_switch.trip(reason, cancel=self.cancel)

    def reconcile(
        self,
        *,
        broker_orders: list[Order] | None = None,
        broker_positions: list[Position] | None = None,
        local_positions: list[Position] | None = None,
    ) -> list[DriftItem]:
        """Compare local state vs broker snapshots. Side-effect free."""
        drifts: list[DriftItem] = []
        if broker_positions is not None:
            local = local_positions
            if local is None:
                local = self._cache.all_positions()
            drifts.extend(
                self._reconciler.reconcile(local, broker_positions),
            )
        if broker_orders is not None:
            drifts.extend(
                self._reconciler.compare_orders(
                    self._cache.all_orders(), broker_orders,
                ),
            )
        # C3: emit reconciliation_drift_total per severity/kind so the
        # reconciliation-drift runbook alert can fire on HIGH+ items.
        if self._metrics is not None and drifts:
            for item in drifts:
                severity = item.severity.value if hasattr(item.severity, "value") else str(item.severity)
                kind = item.kind or "position"
                self._metrics.counter(
                    f"reconciliation_drift_total.{severity.lower()}.{kind}"
                ).inc()
        # Wave C1: persist reconciliation outcome
        self._append(ReconciliationResult(drift_count=len(drifts)))
        return drifts

    def cancel(
        self,
        order_id: OrderId,
        correlation_id: CorrelationId | None = None,
    ) -> Order:
        """Cancel an order and publish ``OrderCancelled`` on the bus.

        ``correlation_id`` (optional) is the cancel mutation's own
        idempotency key (N2): reserved before the venue dispatch, recorded
        on success, released on venue failure, replayed on duplicate
        retries. When omitted (e.g. the kill switch), no reservation is
        made and the previous behavior applies.

        M2: also release the idempotency reservation that was made for
        this order in ``_run_pipeline``. Without this, every cancelled
        order whose cid was reserved leaked a slot in the guard's
        ``_reserved`` set. The reservation is looked up via the
        ``_cid_for_order`` side-table populated by the pipeline; an
        order that never had a cid reserved (e.g. risk-rejected) is
        not in the table and the lookup is a no-op.
        """
        log.info("Cancelling order %s", order_id)
        # N2: reserve the cancel's own idempotency key first. A completed
        # key replays the cancelled Order — no second venue cancel. The
        # replay is answered from the guard alone, so a rebuilt engine
        # (restart with a durable guard) replays without the local cache.
        cid = correlation_id
        if self._guard is not None and cid is not None:
            dup = self._guard.check_and_reserve(
                cid, request_hash=_cancel_fingerprint(order_id),
            )
            if dup is not None:
                log.info("Idempotency replay for cancel correlation %s", cid)
                return dup.result
        try:
            order = self._cache.get_order(order_id.value)
            if order is None:
                raise OrderRejectedError(f"Order {order_id.value} not found")
            # transition_to raises for terminal/illegal states before any
            # venue or OMS write happens.
            cancelled = order.transition_to(OrderStatus.CANCELLED)
            # Wave C1: persist cancel intent before venue dispatch
            self._append(CancelRequested(order_id=order_id.value))
            if _is_bracket_order(order):
                # A bracket is a composite at the venue: cancel the whole
                # super order (entry + protective legs) through the fill
                # source's super-order seam. Venue-first — if the venue
                # rejects, the OMS stays untouched and the error propagates.
                cancel = getattr(self._fill, "cancel_super_order", None)
                if not callable(cancel):
                    raise OrderRejectedError(
                        f"Order {order_id.value} is a bracket and the execution "
                        "source does not support super-order cancellation"
                    )
                cancel(order_id)
            else:
                # Plain live orders: the venue cancel goes through the fill
                # source BEFORE the OMS flips (N1, principal review). Without
                # this, DELETE /orders left the venue order working and the
                # "cancelled" order could still fill. Paper/simulated sources
                # expose a no-op cancel, so OMS-local semantics are unchanged
                # for them. On venue rejection the exception propagates and
                # the OMS cache stays at its pre-cancel status.
                cancel = getattr(self._fill, "cancel", None)
                if callable(cancel):
                    cancel(order_id)
        except Exception:
            # Any failure — unknown order, illegal transition, venue refusal
            # — frees the key for a genuinely new attempt. Never leave the
            # reservation stuck in "reserved".
            if self._guard is not None and cid is not None:
                self._guard.release(cid)
            raise
        self._cache.update_order(cancelled)
        _cancelled_event = OrderCancelled(order=cancelled)
        self._bus.publish(_cancelled_event)
        # Wave C1: persist cancel ack
        self._append(_cancelled_event)
        # M2: release the idempotency reservation that was made for
        # this order. release() is idempotent (discard on a missing
        # key), so this is safe even if the pipeline already released
        # the cid (e.g. on risk rejection before the side-table was
        # populated, or on a non-boundary fill-source error). Pop the
        # side-table so the mapping doesn't outlive the order.
        submit_cid = self._cid_for_order.pop(order_id, None)
        if self._guard is not None and submit_cid is not None:
            self._guard.release(submit_cid)
        # N2: the cancel itself is a completed idempotent request.
        if self._guard is not None and cid is not None:
            self._guard.record_result(cid, cancelled)
        return cancelled

    def modify(self, order_id: OrderId, request: OrderRequest) -> Order:
        """Modify an open order — the third mutation on the canonical spine.

        The broker side is reached through the fill source (live sources
        forward to the venue; simulated/paper/replay are no-ops), then the
        modified fields are projected into the OMS so the cache and the
        venue agree — previously modifications went straight to the broker
        and silently desynced the engine cache.

        H5: re-runs ``RiskManager.check()`` on the modified request
        *before* the broker-side dispatch. An order within limits at entry
        could be modified to exceed ``max_position_value``; without this
        guard the position can grow past the configured cap. The gate
        precedes the venue call so a request risk would deny is never
        sent to the broker (a post-hoc denial would desync the OMS from
        the venue for real money).
        """
        log.info("Modifying order %s", order_id)
        # N2: reserve the modification's idempotency key (bound to the
        # request hash) first. A completed key replays the original
        # modified Order — no second venue dispatch; a key reused with a
        # different payload raises IdempotencyKeyReuseMismatch. The replay
        # is answered from the guard alone, so a rebuilt engine (restart
        # with a durable guard) replays without the local cache.
        cid = request.correlation_id
        if self._guard is not None and cid is not None:
            dup = self._guard.check_and_reserve(
                cid, request_hash=_request_fingerprint(
                    request, operation="modify", target=order_id.value,
                ),
            )
            if dup is not None:
                log.info("Idempotency replay for modify correlation %s", cid)
                return dup.result
        try:
            order = self._cache.get_order(order_id.value)
            if order is None:
                raise OrderRejectedError(f"Order {order_id.value} not found")
            if order.status in _TERMINAL_STATUSES:
                raise OrderRejectedError(
                    f"Order {order_id.value} is {order.status.value} and cannot be modified"
                )
            if request.quantity.value <= order.filled_quantity.value:
                raise OrderRejectedError(
                    f"Order {order_id.value}: modified quantity "
                    f"{request.quantity.value} must exceed already-filled "
                    f"quantity {order.filled_quantity.value}"
                )
            # A bracket modification must carry the full composite so the venue
            # dispatch reaches modify_super_order — never the plain endpoint.
            if _is_bracket_order(order) and not isinstance(request, BracketOrderRequest):
                raise OrderRejectedError(
                    f"Order {order_id.value} is a bracket: modification must carry "
                    "price, stop_loss_price, and target_price"
                )
            # H5: re-run risk on the modified request BEFORE the broker-side
            # dispatch. Rejecting after the venue accepted the modify would
            # leave the OMS rolled back while the venue already changed — a
            # real-money cache/venue desync. Risk first: a denied request
            # never reaches the broker (simulated/paper/replay sources' no-op
            # modify is moot when the request never gets that far).
            if self._risk is not None and not self._risk.check(request):
                raise OrderRejectedError(
                    f"Order {order_id.value}: modified request rejected by risk check"
                )
            modify_fn = getattr(self._fill, "modify", None)
            if callable(modify_fn):
                modify_fn(order_id, request)
            modified = replace(
                order,
                order_type=request.order_type,
                quantity=request.quantity,
                price=request.price,
                trigger_price=request.trigger_price,
                time_in_force=request.time_in_force,
                # Protective legs project onto the OMS record so a modified
                # bracket keeps its identity and reflects the new stop/target.
                stop_loss_price=request.stop_loss_price,
                target_price=request.target_price,
                trailing_jump=request.trailing_jump,
            )
            self._cache.update_order(modified)
            _modified_event = OrderModified(order=modified)
            self._bus.publish(_modified_event)
            # Persist: without this a restart rebuilds the order from its
            # pre-modification snapshot, and the process comes back believing a
            # price or quantity the venue has already changed.
            self._append(_modified_event)
        except Exception:
            # Any failure — unknown order, terminal order, risk denial, venue
            # refusal — frees the key for a genuinely new attempt. Never leave
            # the reservation stuck in "reserved".
            if self._guard is not None and cid is not None:
                self._guard.release(cid)
            raise
        if self._guard is not None and cid is not None:
            self._guard.record_result(cid, modified)
        return modified

    @property
    def kill_switch(self) -> bool:
        """Whether the kill switch is active."""
        return self._kill_switch.active

    @kill_switch.setter
    def kill_switch(self, value: bool) -> None:
        """Activate or deactivate the kill switch."""
        if value:
            self._kill_switch.set()
        else:
            self._kill_switch.clear()

    @property
    def kill_switch_reason(self) -> str:
        """Why the session halted, or "" when it did not."""
        return self._kill_switch.reason

    def get_order(self, order_id: OrderId) -> Order | None:
        """Look up an order by ID."""
        return self._cache.get_order(order_id.value)

    def all_orders(self) -> list[Order]:
        """Return all cached orders."""
        return self._cache.all_orders()

    @property
    def feed_supervisor(self) -> FeedReady | None:
        """Feed readiness gate bound at construction (shared with MarketFeed in live boot)."""
        return self._feed_supervisor

    @property
    def cache(self) -> TradingCache:
        """Access the trading cache."""
        return self._cache


