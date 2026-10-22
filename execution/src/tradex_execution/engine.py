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
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from tradex_domain.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from tradex_domain.errors import OrderRejectedError
from tradex_domain.events import (
    BrokerOrderAcknowledged,
    BrokerOrderRequested,
    CancelRequested,
    DomainEvent,
    ErrorOccurred,
    OrderCancelled,
    OrderFilled,
    OrderModified,
    OrderPlaced,
    OrderRejected,
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
from tradex_domain.order_factory import create_order
from tradex_domain.value_objects import CorrelationId, Money, OrderId
from tradex_observability.tracing import (
    reset_correlation_id,
    set_correlation_id,
    trace_id_of,
)
from tradex_reactive.bus import ReactiveBus
from tradex_reactive.thread_safe_bus import ThreadSafeReactiveBus

from tradex_execution.cash_ledger import CashLedger, CashSnapshot
from tradex_execution.cash_manager import CashManager
from tradex_execution.fee_processor import FeeProcessor
from tradex_execution.fees import (
    FeeCalculator,
    UnsupportedProductTypeError,
    require_fee_schedule,
)
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
from tradex_execution.position_accountant import PositionAccountant
from tradex_execution.reconciliation import (
    AdoptionOutcome,
    DriftItem,
    ReconciliationEngine,
)
from tradex_execution.reconciliation import (
    adopt_venue_positions as _adopt_venue_positions_impl,
)
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
            if request.stop_loss_price is not None else None
        ),
        _canonical_decimal(
            request.target_price.value
            if request.target_price is not None else None
        ),
        _canonical_decimal(
            request.trailing_jump.value
            if request.trailing_jump is not None else None
        ),
    ])


def _cancel_fingerprint(order_id: OrderId) -> str:
    """Canonical fingerprint for a cancel mutation."""
    return f"cancel|{order_id.value}"

class ExecutionEngine:
    """Single order spine as reactive pipeline.

    idempotency → risk → fill → OMS, all as Observable operators.

    **One entry point (AC-22).** ``submit(request)`` is the ONLY way an order
    reaches the venue. This used to be two doors: the imperative ``submit()``
    and a CQRS door where a strategy published ``PlaceOrderCommand`` on the
    public bus and the engine picked it up with a subscription. The CQRS door
    is gone, and with it the ability to reach the venue by publishing on a bus
    anyone holds a reference to. A strategy still turns a ``Signal`` into a
    fully-formed ``OrderRequest`` — it just hands that request to the engine
    rather than to the bus, so the sizing and protective-pair gates in
    ``ReactiveStrategyEngine`` run on the way in (AC-22 closed the *engine's*
    door, not the strategy's two gates: the old bus door converged on this
    same pipeline, so engine risk/kill-switch/feed/idempotency always ran;
    what a raw bus publisher could skip was ``PositionSizer`` and
    ``protective_request``).

    The engine still *subscribes* to the bus for the inbound fill bridge
    (``OrderFilled``) and for the lifecycle events it mirrors to durable
    storage. Those are reads of the venue's truth, not doors to it: a
    published ``OrderFilled`` is applied idempotently to the book and never
    dispatches anything. The bus is an observation surface, not a command
    surface.
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
        feed_supervisor: FeedReady | None = None,
        event_store: EventStore | None = None,
        require_risk: bool = False,
        require_feed: bool = False,
        require_durable_events: bool = False,
        reconciler: ReconciliationEngine | None = None,
        fill_dedup: FillDedup | None = None,
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
        self._feed_supervisor = feed_supervisor
        self._event_store = event_store
        self._require_risk = require_risk
        self._require_feed = require_feed
        self._require_durable_events = require_durable_events
        self._guard_lock = threading.Lock()
        self._fee_calculator = fee_calculator
        self._fee_processor = FeeProcessor(fee_calculator)
        #: The cash floor policy, retained because a ledger can also be created
        #: long after construction by ``restore_cash`` (the live/paper seed).
        #: That ledger has to be born under the same policy the constructor
        #: applies, or the engine's cash gate is armed in the backtest and
        #: disarmed in production — see ``restore_cash``.
        self._allow_negative_cash = allow_negative_cash
        self._cash_manager = CashManager(
            append=self._append, allow_negative=allow_negative_cash,
        )
        if cash is not None:
            self._cash_manager.restore(cash)
        self._order_manager = OrderManager(self._cache)
        self._position_manager = PositionAccountant(self._cache)
        self._kill_switch = KillSwitch(metrics=metrics, risk=risk_manager, cache=self._cache)
        self._reconciler = reconciler if reconciler is not None else ReconciliationEngine()
        #: Bounded LRU dedup for applied fill events — re-published broker
        #: fills are skipped, distinct partial fills are each applied in full.
        #: Eviction is observable via the ``bus.applied_fills.evicted`` counter.
        self._fill_dedup = fill_dedup if fill_dedup is not None else FillDedup(
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
        self._cash_manager.reset_anchor()
        if self._cash_ledger is not None:
            # An explicit ``cash=`` IS a new opening balance, so it anchors.
            self.restore_cash(self._cash_ledger.cash, re_anchor=True)

    def _setup_pipeline(self) -> None:
        """Wire the reactive order pipeline using RxPY operators.

        Deliberately subscribes to NO order-intake event (AC-22). The engine
        used to subscribe to both ``OrderRequest`` and ``PlaceOrderCommand``
        here, which made the public bus a second door to the venue: anything
        holding a bus reference could publish an order and have it processed
        without going through :meth:`submit`. Neither subscription has a
        legitimate publisher left — every order in this repository already
        came from ``submit()`` or from a strategy handing its request to the
        engine — so both are removed rather than merely discouraged. Removing
        them is what makes ``submit()`` the only path *in fact* and not just
        in documentation.

        The remaining subscription is the inbound fill bridge, which is a read
        of the venue's truth and not a door to it.
        """

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
        reserved_cid: CorrelationId | None = None,
    ) -> OrderReceipt | None:
        """Publish a terminal intake rejection (feed/risk/kill-switch gates).

        ``reserved_cid`` is the idempotency key this request already reserved.
        Gates that run *before* ``check_and_reserve`` have nothing to release and
        leave it ``None``; a gate that runs after must hand the key back, or the
        refusal leaks the correlation id for the life of the process and every
        retry of it answers ``idempotency_in_flight`` as a duplicate (M2).
        """
        if metric_suffix is not None and self._metrics is not None:
            self._metrics.counter(
                f"orders.rejection_reason.{metric_suffix}",
            ).inc()
        if reserved_cid is not None and self._guard is not None:
            with self._guard_lock:
                self._guard.release(reserved_cid)
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
        # Only the inbound fill bridge remains subscribed (AC-22 removed the
        # order-intake subscriptions), so this is the sole disposable to tear
        # down. The kill switch above is what stops further submits.
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
        """Process a single order request through the pipeline, fire-and-forget.

        Reachable only via :meth:`submit` with ``sync=False`` (the ordering
        API used by code that must not block a caller, e.g. an HTTP handler
        that returns a 202). Since AC-22 there is no bus subscription feeding
        this: a published ``OrderRequest`` is inert.
        """
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
        """Trace wrapper: pin the order's trace id for the whole pipeline.

        The idempotency→risk→fill→OMS sequence below is the one place every
        order passes through, whatever door it came in by (AC-22: there is now
        only one — :meth:`submit`, but the plumbing stays door-agnostic so a
        future door cannot accidentally bypass this body). Binding the domain
        ``CorrelationId`` into the logging contextvar here means every
        log line the engine emits while this order is in flight — the risk gate,
        the fee-schedule refusal, the durable-append failures, the venue call —
        is stamped with the same id the order is persisted under, so one grep
        returns the whole story of that order.

        The contextvar is reset in a ``finally`` so the next unit of work on this
        thread (the bus delivers a stream of independent orders) does not
        inherit the previous order's id.
        """
        token = set_correlation_id(trace_id_of(request.correlation_id))
        try:
            self._log_trace_point("order.submit", request, sync=sync)
            return self._run_pipeline_body(request, sync=sync)
        finally:
            reset_correlation_id(token)

    def _log_trace_point(
        self,
        stage: str,
        request: OrderRequest,
        **fields: object,
    ) -> None:
        """Emit one greppable chain point for ``request``'s trace id.

        The correlation id rides as a real structured field (``extra``), not
        interpolated into the message, so the JSON formatter emits it as a
        queryable key.
        """
        log.info(
            "trace %s instrument=%s",
            stage,
            request.instrument.instrument_id,
            extra={
                "stage": stage,
                "instrument": request.instrument.instrument_id,
                "correlation_id": trace_id_of(request.correlation_id),
                **fields,
            },
        )

    def _run_pipeline_body(
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
            # M2: release the reservation on risk-reject so the cid is
            # immediately reusable. Risk rejection is a terminal state for
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

        # 2b. Fee schedule gate (AC-6). The product must be *priceable* before
        # anything leaves this process: `fees.require_fee_schedule` refuses
        # MARGIN/MTF/COVER_ORDER because no rate table exists for them, and the
        # alternatives were a silent zero or the intraday table, both of which
        # book a real order against a cost nobody computed.
        #
        # Placed after the risk block and BEFORE the dispatch-intent write and
        # the venue call, because the cost of refusing late is the thing the
        # kill switch exists for: an order the venue has accepted, that this
        # process can neither price nor account for, filed as a plain
        # REJECTED receipt with no UNKNOWN anywhere in it.
        #
        # It is a rejection and not a raise, for the same reason the risk gate
        # is: the idempotency reservation is already made at this point, and a
        # bare raise would strand the correlation id so the venue's retry is
        # then skipped as a duplicate. The key is handed back with the receipt.
        try:
            require_fee_schedule(request.product_type)
        except UnsupportedProductTypeError as exc:
            log.warning("Fee schedule gate refused %s: %s",
                        request.instrument, exc)
            return self._reject_intake(
                request,
                str(exc),
                sync=sync,
                metric_suffix="fee_schedule_unsupported",
                reserved_cid=reserved_cid,
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
            # `reaches_venue` is the fail-safe axis, not the latch. A source
            # that can leave this process is assumed to have crossed unless it
            # says otherwise, so a live source that raises before it manages to
            # set `submission_boundary_crossed` is still filed UNKNOWN. Treating
            # it as REJECTED is what let a client retry place a second live
            # order under one correlation id: the retry is safe only if the
            # first attempt provably never reached the venue. Offline sources
            # (backtest/paper/replay) declare reaches_venue=False, so a local
            # failure there is still a definitive rejection.
            from tradex_domain.errors import OrderSubmissionUnknownError

            # Three states, not two. A source that can leave this process is
            # assumed to have crossed the venue boundary *unless it proves
            # otherwise*:
            #   latch absent  -> we do not know -> UNKNOWN (fail safe)
            #   latch True    -> crossed        -> UNKNOWN
            #   latch False   -> provably local -> REJECTED
            # The third state is what commit 6191b9e needed: the broker adapter
            # validates lot size, capability gates and the live-order gate
            # *before* sending, and those must stay rejections rather than
            # halting the book. Only a source that never sets the latch at all
            # is treated as unknown, which is the case that placed a second
            # live order under one correlation id.
            _ABSENT = object()
            may_have_reached_venue = getattr(self._fill, "reaches_venue", True)
            latch = getattr(self._fill, "submission_boundary_crossed", _ABSENT)
            crossed_or_unknown = latch is _ABSENT or latch is True
            boundary_crossed = (
                may_have_reached_venue and crossed_or_unknown
            ) or isinstance(exc, OrderSubmissionUnknownError)
            if boundary_crossed:
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
                failures = self._kill_switch.trip(
                    "unknown_broker_outcome: the venue may have accepted an "
                    f"order this process cannot account for ({exc})",
                    cancel=self.cancel,
                )
                if failures:
                    log.error("kill-switch could not cancel orders: %s", failures)
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
        # M2: stamp the order_id → cid mapping so cancel() can release the
        # reservation. The order has just been created; from here forward the
        # cid is owned by this order. record_result() removes the entry from
        # the reserved set but does not clear the side-table, so cancel() needs
        # the cid even after the key is completed.
        #
        # The key is deliberately NOT completed here. It is completed at the
        # end of the fill block below, once the money has actually moved;
        # completing it now meant a mid-block failure left the client replaying
        # a receipt for a partly-applied order (M-3).
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
                recorded = getattr(self._fill, "recorded_fee_for", None)
                charged_fee = self._fee_processor.fee_for_fill(fill, recorded)
            try:
                order = self._apply_fill_to_book(order, fill, charged_fee, cid)
            except Exception:
                # The fill did not take effect. Release the reservation so the
                # client can retry: leaving it reserved makes every later
                # attempt answer PENDING/idempotency_in_flight forever, and
                # leaving it completed (the previous behaviour) replays a
                # receipt for an order that never applied (M-3).
                if reserved_cid is not None and self._guard is not None:
                    with self._guard_lock:
                        self._guard.release(reserved_cid)
                raise
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
                    self._guard.record_result(
                        reserved_cid,
                        OrderReceipt(
                            order_id=order.order_id,
                            status=order.status,
                            message="submitted",
                        ),
                    )
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
        """Set the cash ledger's balance to an absolute figure.

        Delegates to :class:`CashManager`, which owns the ledger and anchor
        state. See ``CashManager.restore`` for the full contract.
        """
        self._cash_manager.restore(cash, total_fees, re_anchor=re_anchor)

    @property
    def accrued(self) -> dict[str, Decimal]:
        """Per-order brokerage accrual, owned by the FeeProcessor (H2 replay state)."""
        return self._fee_processor.accrued

    @property
    def _cash_ledger(self) -> CashLedger | None:
        """The cash balance ledger — delegated from its owner, the CashManager.

        Reads only: the engine never constructs or mutates a ledger directly
        (CashManager owns creation, anchoring and restore).
        """
        return self._cash_manager.balance_ledger

    @property
    def cash_anchor_missing(self) -> bool:
        """True when no opening anchor has been written for this ledger."""
        return self._cash_manager.anchor_missing

    def cash_snapshot(self) -> CashSnapshot:
        """Return reactive cash state, or a disabled zero snapshot."""
        return self._cash_manager.snapshot()

    def _apply_fill_to_book(
        self, order: Order, fill: Fill, charged_fee: Decimal, cid: CorrelationId | None
    ) -> Order:
        """Make one fill's effect real: book, charge, advance the OMS, publish.

        Split out of ``_run_pipeline`` so the whole effect is one unit the
        caller can compensate on: anything raised here means the fill did not
        take effect, and the caller releases the idempotency reservation so
        the request stays retryable (M-3). Returns the OMS order after the
        fill transition (FILLED/PARTIALLY_FILLED) so a sync receipt reports
        the terminal state rather than the pre-fill ACK.
        """
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
            return order

        if not getattr(self._fill, "position_projection_owned", False):
            # Position, notional and fee as ONE operation under the ledger's
            # single lock (AC-21). The affordability proof now lives inside
            # apply_fill, so it cannot be separated from the debits it guards
            # by a concurrent submit that read the same balance.
            self._apply_fill_effect(fill, charged_fee)
        order = self._order_manager.on_order_filled(order, fill)
        # G3: record the fingerprint in the applied-fills set BEFORE
        # publishing so that any re-publish from the live-fill bridge
        # (which subscribes to OrderFilled) is short-circuited by
        # the dedup check in _apply_fill.
        self._record_applied_fill(fill)
        self._bus.publish(_filled_event)
        # The idempotency key is completed only here, once the money has
        # actually moved. Completing it before this point meant a mid-block
        # failure left the client replaying a receipt for a partly-applied
        # order, making the divergence permanent (M-3).
        if self._guard is not None and cid is not None:
            self._guard.record_result(
                cid,
                OrderReceipt(
                    order_id=order.order_id,
                    status=order.status,
                    message="submitted",
                ),
            )
        return order

    def _require_cash_for_fill(self, fill: Fill, charged_fee: Decimal) -> None:
        """Prove the ledger can pay for *fill* before anything is booked.

        The requirement is the notional for a BUY and the fee alone for a SELL,
        checked as ONE amount and not two, so the debits that follow cannot
        fail: the position is already booked by then, and a failure there
        leaves a position with no cash movement behind it and an OMS reading
        FILLED (M-3). Checking the sum up front is what makes booking, notional
        and fee one operation that either all happens or does not (AC-2).

        The fee is part of the requirement, not a rounding detail: the last
        order an account can afford is the one its own fee makes unaffordable,
        so a check on the notional alone would pass a fill that its own debit
        then refuses.

        Both fill doors prove affordability the same way, and they must: this
        bug existed on the second door precisely because it had been fixed on
        the first and the two copies drifted apart. The inbound bus bridge
        (``_apply_fill``) calls this directly, because there the proof must run
        ahead of the durable write so a refused fill never reaches the log. The
        synchronous pipeline (``_apply_fill_to_book``) appends first and then
        hands the same formula to ``CashLedger.apply_fill`` as its first step,
        so the proof and the debits it guards run under one lock (AC-21) and
        cannot be separated by a concurrent submit. One formula, two doors,
        one lock.

        A SELL is held to its fee against the balance as it stands, before
        this fill's own credit lands, so a sell out of an empty account is
        refused on a fee its credit would have covered. That is the formula the
        synchronous door has always used and it is reused verbatim rather than
        re-decided per door; it fails closed, and it turns away only the case
        where the account holds no cash at all.
        """
        if self._cash_ledger is None:
            return
        notional = fill.quantity.value * fill.price.value
        debit = notional if fill.side is OrderSide.BUY else Decimal(0)
        self._cash_ledger.require_debit(debit + charged_fee, f"fill {fill.order_id}")

    def _apply_fee(self, fill: Fill, charged: Decimal | None = None) -> None:
        """Deduct *fill*'s fee from the position's realized P&L and cash.

        *charged* is the already-computed amount from :class:`FeeProcessor`;
        omit it to compute and apply in one step.

        Failures propagate loudly — a silently-swallowed fee bug is exactly the
        accounting divergence the parity work exists to prevent.
        """
        if charged is None:
            charged = self._fee_processor.compute_fee(fill)
        if charged <= 0:
            return
        fee = Money(amount=charged)
        self._position_manager.on_fee(fill, fee)
        if self._cash_ledger is not None:
            self._cash_ledger.on_fee(fee)

    def _book_position(self, fill: Fill, charged_fee: Decimal) -> None:
        """Position-booker mutations for one fill: the fill, then its fee.

        Runs as ``CashLedger.apply_fill``'s ``book`` hook, i.e. while the single
        ledger lock is held, so the position, the notional debit and the fee
        debit are one operation (AC-21). The fee also lands in the position's
        realised P&L; that is a position-booker mutation, not a cash one, and it
        rides here so the whole per-fill position update sits inside the one lock.
        """
        self._position_manager.on_fill(fill)
        if charged_fee > 0:
            self._position_manager.on_fee(fill, Money(amount=charged_fee))

    def _apply_fill_effect(self, fill: Fill, charged_fee: Decimal) -> None:
        """Apply one fill's whole effect -- position, notional, fee -- atomically.

        Delegates to ``CashLedger.apply_fill``, the single operation by which a
        fill moves money. It holds the ledger's one lock across the affordability
        proof, this position booking, the notional debit and the fee debit, so two
        concurrent submits can never both pass the affordability check and both
        debit (AC-21). The proof inside apply_fill is the same
        ``require_debit`` check both doors and the negative controls exercise, so
        an unaffordable fill is still refused whole -- before ``_book_position``
        runs, so it books nothing and moves no cash.

        When the engine holds no ledger (``cash=`` was never supplied and
        ``restore_cash`` has not run) there is no cash state to guard, so the
        position booking and its fee run directly -- the position booker's own
        per-instrument lock still serialises them. This is the historical no-gate
        mode and its behaviour is unchanged.
        """
        if self._cash_ledger is None:
            self._position_manager.on_fill(fill)
            self._apply_fee(fill, charged_fee)
            return
        self._cash_ledger.apply_fill(
            fill.side,
            fill.quantity,
            fill.price,
            charged_fee,
            f"fill {fill.order_id}",
            book=lambda: self._book_position(fill, charged_fee),
        )

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
        return create_order(request, str(uuid.uuid4()), status=status)

    def _record_applied_fill(self, fill: Any) -> bool:
        """Insert the fill's fingerprint into the dedup LRU.

        Delegates to FillDedup. Returns True if duplicate (skip apply).
        """
        return self._fill_dedup.check_and_record(fill)

    def _seen_applied_fill(self, fill: Any) -> bool:
        """True when this exact fill was already applied (without recording)."""
        return self._fill_dedup.seen(fill)

    def ingest_fill(self, fill: Fill, fee_amount: Decimal | None = None) -> None:
        """Apply a fill that originated OUTSIDE this process (AC-22).

        The named, public door behind :class:`~tradex_domain.protocols.FillIngestPort`.
        It exists because the inbound path was previously reachable only by
        publishing ``OrderFilled`` on the public bus, which made a *mutation*
        surface out of a surface documented as observation-only: anyone holding
        a bus reference could change the book, and nothing distinguished that
        from the legitimate live bridge doing the same thing.

        Behaviour is identical to the bridge's — ``_apply_fill`` is the one
        implementation, and this simply names the caller instead of requiring it
        to hand-roll the event. A caller that already has an ``OrderFilled``
        should use the bus as before; this is for a caller that holds a bare
        ``Fill`` and the fee the venue actually charged.
        """
        self._apply_fill(OrderFilled(fill=fill, fee_amount=fee_amount))

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

        Affordability is proved before anything is booked, so an unaffordable
        fill is refused whole rather than half applied: no position, no cash
        movement, no durable record and no committed fingerprint, which is what
        makes a re-delivery of a refused fill still raise rather than vanish
        into the dedup. Booking, the notional debit and the fee debit are then
        one operation that cannot fail (AC-2).
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
                else self._fee_processor.compute_fee(fill)
            )
        owns_projection = getattr(self._fill, "position_projection_owned", False)
        # AC-21: hold the ledger's ONE lock across this door's entire critical
        # section -- the affordability proof, the durable append, the dedup
        # fingerprint and the booking/debits. Without the lock a concurrent
        # submit could pass this fill's proof on a balance the other fill was
        # about to consume, or slip between the append and the debits. With no
        # ledger there is no cash state to guard (the proof is already a no-op),
        # so the section runs unserialised exactly as before.
        scope = (
            self._cash_ledger.transaction()
            if self._cash_ledger is not None
            else nullcontext()
        )
        with scope:
            if not owns_projection:
                # Prove affordability BEFORE booking anything, and BEFORE the two
                # records below that would make this fill un-refusable. The debits
                # that follow are notional and then fee, each of which can trip the
                # ledger's buying-power floor; the position is already booked by
                # then, so a failure there leaves a position with no cash movement
                # and the raise swallowed by the bus's subscriber isolation -- the
                # live book and the recovered book disagreeing for good (AC-2).
                #
                # This sits ahead of the durable write and the dedup fingerprint,
                # which is where the synchronous door differs and why: a refused
                # fill in the log is folded by recovery into a position this book
                # never had, and a refused fill whose fingerprint is committed has
                # its own re-delivery skipped as a duplicate, so the fill is lost
                # with no error at all. Neither trace may exist for a fill that did
                # not apply. ``require_debit`` only reads, so "persist before
                # mutating" still holds for the first mutation, which is the
                # booking below.
                #
                # The failure is republished as ``ErrorOccurred`` because
                # ``ReactiveBus._isolate`` logs and drops a subscriber's exception:
                # without this the raise stops at a log line and a run fed this
                # door quietly loses a fill. ``BacktestEngine.run`` treats it as
                # run-ending; the live boot logs and counts it.
                try:
                    self._require_cash_for_fill(fill, charged_fee)
                except ValueError as exc:
                    self._bus.publish(ErrorOccurred(error=exc))
                    raise
            # Persist BEFORE mutating -- same invariant as the sync fill path, and
            # still ahead of the booking. It sits inside the ledger lock so the
            # whole proof->append->book sequence is atomic against other submitters.
            if not self._append(OrderFilled(fill=fill, fee_amount=charged_fee)):
                log.error(
                    "Inbound fill not durable; leaving the book unchanged for %s",
                    fill.order_id,
                )
                return
            # The write landed, so this fill is now applied and must dedup.
            self._record_applied_fill(fill)
            if not owns_projection:
                # Position, notional and fee as ONE operation under the same lock
                # that proved affordability above (AC-21).
                self._apply_fill_effect(fill, charged_fee)
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
                create_order(
                    OrderRequest(
                        instrument=fill.instrument,
                        side=fill.side,
                        order_type=OrderType.MARKET,
                        quantity=fill.quantity,
                        price=fill.price,
                        time_in_force=TimeInForce.DAY,
                        correlation_id=getattr(fill, "correlation_id", None),
                        tag=getattr(fill, "tag", None),
                    ),
                    fill.order_id.value,
                    filled=fill.quantity.value,
                    status=OrderStatus.FILLED,
                )
            )
        log.info(
            "Inbound fill applied: %s qty=%s price=%s (engine fill bridge)",
            fill.order_id, fill.quantity, fill.price,
        )

    def submit(self, request: OrderRequest) -> OrderReceipt:
        """Synchronous submit — the ONLY door to the venue (AC-22).

        Creates the order, runs the fill source, updates OMS, and
        publishes events. Returns an OrderReceipt immediately.

        There is no CQRS alternative: the bus is no longer an order-intake
        surface, so a caller that wants an order at the venue calls this.
        Any code path that reaches the venue without passing through here
        does not exist in this repository.

        A failure is BOTH raised to the caller and published as
        ``ErrorOccurred``. That is not belt-and-braces: before AC-22 the bus
        door published the event and ``submit()`` raised, and operators
        watching the bus were told about a halt they would otherwise have seen
        only as an exception in someone else's stack. With one door, one
        channel would have quietly dropped that signal, so the event is
        published here and the raise is kept — the two consumers are
        different (a live bus subscriber vs the calling code) and neither is
        served by the other.
        """
        submit_start = time.perf_counter()
        try:
            return self._submit_impl(request)
        except Exception as exc:
            # ``ReactiveBus._isolate`` logs and drops a subscriber's exception,
            # so an unpublished failure stops at a log line. Republish it the
            # way the reactive intake path always did before AC-22 removed it.
            self._bus.publish(ErrorOccurred(error=exc))
            raise
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
        record: bool = True,
    ) -> list[DriftItem]:
        """Compare local state vs broker snapshots. Side-effect free.

        ``record=False`` compares without appending a
        :class:`ReconciliationResult`. The compare itself was always
        side-effect free; the append is bookkeeping, and a caller that runs a
        comparison on a timer (the lane 5e reconciliation monitor, every 30
        seconds) does not want one durable marker per poll forever — 2,880
        rows a day that all say "nothing was found".

        The default stays ``True``, and *unconditionally*: the marker is what
        the safety surface's ``last_checked_at`` is derived from, so a clean
        run that appended nothing would leave a healthy session reporting
        "never reconciled" — a worse lie than a redundant row. A caller that
        suppresses it must record the check itself (the monitor seam calls
        ``session.record_reconciliation``, and the safety surface falls back to
        that timestamp), so a suppressed run is still visible.
        """
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
                severity = (
                    item.severity.value
                    if hasattr(item.severity, "value")
                    else str(item.severity)
                )
                kind = item.kind or "position"
                self._metrics.counter(
                    f"reconciliation_drift_total.{severity.lower()}.{kind}"
                ).inc()
        # Wave C1: persist reconciliation outcome. Unconditional by default:
        # this marker is the safety surface's "last checked" evidence, and a
        # clean run that leaves no trace reads as "never reconciled". A timer
        # caller that opts out with record=False records the check itself.
        if record:
            self._append(ReconciliationResult(drift_count=len(drifts)))
        return drifts

    def adopt_venue_positions(
        self,
        *,
        broker: Any,
        local_positions: Sequence[Any] | None = None,
        cache: Any = None,
        unsettled_instruments: frozenset[str] | set[str] | None = None,
        known_instruments: Sequence[str] | None = None,
        last_read_at: datetime | None = None,
        now: datetime | None = None,
        adopt: bool = True,
        read: Any = None,
    ) -> AdoptionOutcome:
        """Adopt the venue's positions into this engine's book, or refuse.

        REF-12: the lane-5e adoption used to live in the HTTP edge
        (``tradex_interfaces.safety``); this method is its public entry. The
        flow — sealed venue read, refusal gates (failed / partial / stale /
        unsettled-guard), pure plan derivation, and the batch-atomic write —
        is :func:`tradex_execution.reconciliation.adopt_venue_positions`; this
        method binds it to the engine's own book. The engine has no broker of
        its own, so *broker* stays an explicit argument: a caller has to ask,
        which is the adoption's "explicit, never automatic" contract.

        The cache and the local book default to this engine's, so the common
        call is ``engine.adopt_venue_positions(broker=broker)``; the remaining
        arguments pass through unchanged (see the implementation for each
        guard's rationale). ``cache=`` accepts a stand-in for tests, exactly as
        the plain function always has.
        """
        return _adopt_venue_positions_impl(
            broker=broker,
            local_positions=(
                local_positions
                if local_positions is not None
                else self._cache.all_positions()
            ),
            cache=cache if cache is not None else self._cache,
            unsettled_instruments=unsettled_instruments,
            known_instruments=known_instruments,
            last_read_at=last_read_at,
            now=now,
            adopt=adopt,
            read=read,
        )

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


