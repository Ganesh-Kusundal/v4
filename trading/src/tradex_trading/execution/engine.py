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
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from tradex_domain.enums import OrderStatus
from tradex_domain.errors import OrderRejectedError
from tradex_domain.events import (
    ErrorOccurred,
    OrderFilled,
    OrderPlaced,
    OrderRejected,
    PlaceOrderCommand,
)
from tradex_domain.execution import Order, OrderReceipt, OrderRequest, Position
from tradex_domain.value_objects import CorrelationId, OrderId

from tradex_trading.execution.fill_sources import FillSource
from tradex_trading.execution.order_manager import OrderManager
from tradex_trading.execution.position_manager import PositionManager
from tradex_trading.execution.reconciliation import DriftItem, ReconciliationEngine
from tradex_trading.execution.trading_cache import TradingCache

if TYPE_CHECKING:
    from tradex_trading.runtime.metrics import MetricsRegistry

log = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.UNKNOWN,
})


# ---------------------------------------------------------------------------
# Risk gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RiskCheckResult:
    """Outcome of a risk check — approved flag plus human-readable reason."""

    approved: bool
    reason: str = ""


# ---------------------------------------------------------------------------
# Order store
# ---------------------------------------------------------------------------


@runtime_checkable
class OrderStore(Protocol):
    """Persistence abstraction for orders."""

    def upsert(self, order: Order) -> None: ...
    def get(self, order_id: OrderId) -> Order | None: ...
    def all_orders(self) -> list[Order]: ...


class InMemoryOrderStore:
    """Dict-backed OrderStore for tests and single-process use."""

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}

    def upsert(self, order: Order) -> None:
        self._orders[order.order_id.value] = order

    def get(self, order_id: OrderId) -> Order | None:
        key = order_id.value if isinstance(order_id, OrderId) else str(order_id)
        return self._orders.get(key)

    def all_orders(self) -> list[Order]:
        return list(self._orders.values())


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IdempotencyDuplicate:
    """A completed idempotent request — its recorded result is returned."""

    result: Any


@runtime_checkable
class IdempotencyGuard(Protocol):
    def check_and_reserve(
        self, correlation_id: CorrelationId,
    ) -> IdempotencyDuplicate | None: ...
    def record_result(
        self, correlation_id: CorrelationId, result: Any,
    ) -> None: ...
    def release(self, correlation_id: CorrelationId) -> None: ...


class MemoryIdempotencyGuard:
    """In-process correlation-id dedupe with reservation + release."""

    def __init__(self) -> None:
        self._reserved: set[str] = set()
        self._completed: dict[str, Any] = {}
        self._lock = threading.RLock()

    def check_and_reserve(
        self, correlation_id: CorrelationId,
    ) -> IdempotencyDuplicate | None:
        key = str(correlation_id.value)
        with self._lock:
            if key in self._completed:
                return IdempotencyDuplicate(result=self._completed[key])
            if key in self._reserved:
                raise RuntimeError(
                    f"idempotency key is already reserved: {key}",
                )
            self._reserved.add(key)
            return None

    def record_result(
        self, correlation_id: CorrelationId, result: Any,
    ) -> None:
        key = str(correlation_id.value)
        with self._lock:
            self._completed[key] = result
            self._reserved.discard(key)

    def release(self, correlation_id: CorrelationId) -> None:
        with self._lock:
            self._reserved.discard(str(correlation_id.value))


class RiskManager:
    """Simple risk manager with configurable limits."""

    def __init__(
        self,
        max_order_value: Decimal | None = None,
        max_position_value: Decimal | None = None,
        max_orders_per_minute: int | None = None,
        *,
        live_orders_enabled: bool = True,
    ) -> None:
        self._max_order_value = max_order_value
        self._max_position_value = max_position_value
        self._max_orders_per_minute = max_orders_per_minute
        self._recent_orders: deque[datetime] = deque()
        self._lock = threading.Lock()
        self._live_orders_enabled = live_orders_enabled

    @property
    def live_orders_enabled(self) -> bool:
        """Master gate: when False, all orders are rejected."""
        return self._live_orders_enabled

    @live_orders_enabled.setter
    def live_orders_enabled(self, value: bool) -> None:
        self._live_orders_enabled = value

    def check(self, request: OrderRequest) -> bool:
        """Return True if the order passes risk checks, False to reject."""
        with self._lock:
            # Master gate
            if not self._live_orders_enabled:
                return False

            # Order value check
            if self._max_order_value is not None and request.price is not None:
                order_value = request.price.value * request.quantity.value
                if order_value > self._max_order_value:
                    return False

            # Rate limit check
            if self._max_orders_per_minute is not None:
                now = datetime.now(UTC)
                # Purge old entries
                while self._recent_orders and (now - self._recent_orders[0]).total_seconds() > 60:
                    self._recent_orders.popleft()
                if len(self._recent_orders) >= self._max_orders_per_minute:
                    return False
                self._recent_orders.append(now)

            return True

    def check_order(self, request: OrderRequest, context: Any = None) -> RiskCheckResult:
        """v3-parity risk check returning rich result."""
        approved = self.check(request)
        return RiskCheckResult(
            approved=approved,
            reason="" if approved else "risk_check_failed",
        )


class ExecutionEngine:
    """Single order spine as reactive pipeline.

    idempotency → risk → fill → OMS, all as Observable operators.
    This is the core of v4 — the reactive execution pipeline.
    """

    def __init__(
        self,
        bus: Any,  # ReactiveBus
        fill_source: FillSource,
        risk_manager: RiskManager | None = None,
        idempotency_guard: Any | None = None,
        cache: TradingCache | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._bus = bus
        self._fill = fill_source
        self._risk = risk_manager
        self._guard = idempotency_guard
        self._cache = cache or TradingCache()
        self._metrics = metrics
        self._order_manager = OrderManager(self._cache)
        self._position_manager = PositionManager(self._cache)
        self._kill_switch = threading.Event()
        self._reconciler = ReconciliationEngine()
        self._setup_pipeline()

    def _setup_pipeline(self) -> None:
        """Wire the reactive order pipeline using RxPY operators.

        Subscribes to OrderRequest messages on the bus and processes them
        through the pipeline: idempotency → risk → fill → OMS update → publish.
        Also subscribes to PlaceOrderCommand for CQRS-style order submission.
        """
        from rx import operators as ops

        self._pipeline_disposable = self._bus.of_type(OrderRequest).pipe(
            ops.filter(lambda _: not self._kill_switch.is_set()),
        ).subscribe(
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
        finally:
            if self._metrics is not None:
                self._metrics.histogram("orders.process_latency_seconds").observe(
                    time.perf_counter() - t0,
                )

    def _process_request_impl(self, request: OrderRequest) -> None:
        """Inner pipeline logic — separated for latency instrumentation."""
        log.info("Processing order request for %s", request.instrument)

        # 1. Idempotency check
        if self._guard is not None:
            cid = request.correlation_id
            if cid is not None:
                dup = self._guard.check_and_reserve(cid)
                if dup is not None:
                    # v3 parity: silently replay — original events were already published
                    log.info("Idempotency replay for correlation %s", cid)
                    if self._metrics is not None:
                        self._metrics.counter("orders.idempotency_replay").inc()
                    return

        # 2. Kill switch
        if self._kill_switch.is_set():
            return

        # 3. Risk check
        if self._risk is not None and not self._risk.check(request):
            log.warning("Risk check failed for order")
            order = self._make_order(request, OrderStatus.REJECTED)
            self._order_manager.on_order_created(order)
            self._bus.publish(OrderRejected(order=order, reason="risk_check_failed"))
            if self._metrics is not None:
                self._metrics.counter("orders.rejected").inc()
                self._metrics.counter("risk.rejected").inc()
            return

        # 4. Fill
        try:
            order, fill = self._fill.submit(request)
        except Exception as exc:
            boundary_crossed = getattr(
                self._fill, "submission_boundary_crossed", False,
            )
            if boundary_crossed:
                from tradex_domain.errors import OrderSubmissionUnknownError
                raise OrderSubmissionUnknownError(
                    f"Order submission failed after crossing broker boundary: {exc}"
                ) from exc
            if self._guard is not None and request.correlation_id is not None:
                self._guard.release(request.correlation_id)
            order = self._make_order(request, OrderStatus.REJECTED)
            self._order_manager.on_order_created(order)
            self._bus.publish(OrderRejected(order=order, reason=str(exc)))
            if self._metrics is not None:
                self._metrics.counter("orders.rejected").inc()
            return

        # 5. OMS update
        self._order_manager.on_order_created(order)
        self._bus.publish(OrderPlaced(order=order))

        if fill is not None:
            # Guard: skip position update if fill source owns projection
            if not getattr(self._fill, "position_projection_owned", False):
                self._position_manager.on_fill(fill)
            self._order_manager.on_order_filled(order, fill)
            self._bus.publish(OrderFilled(fill=fill))
            # Record idempotency result for replay
            if self._guard is not None and request.correlation_id is not None:
                self._guard.record_result(
                    request.correlation_id, order.order_id,
                )
            log.info(
                "Order filled: %s qty=%s price=%s",
                order.order_id, fill.quantity, fill.price,
            )
            if self._metrics is not None:
                self._metrics.counter("orders.submitted").inc()
                self._metrics.counter("orders.filled").inc()
        else:
            if self._metrics is not None:
                self._metrics.counter("orders.submitted").inc()

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
        )

    def submit(self, request: OrderRequest) -> OrderReceipt:
        """Synchronous submit — bridges to reactive pipeline.

        Creates the order, runs the fill source, updates OMS, and
        publishes events. Returns an OrderReceipt immediately.
        """
        t0 = time.perf_counter()
        try:
            return self._submit_impl(request)
        finally:
            if self._metrics is not None:
                self._metrics.histogram("orders.submit_latency_seconds").observe(
                    time.perf_counter() - t0,
                )

    def _submit_impl(self, request: OrderRequest) -> OrderReceipt:
        """Inner submit logic — separated for latency instrumentation."""
        log.info("Sync submit: %s %s %s", request.side, request.quantity, request.instrument)

        # Kill switch short-circuit
        if self._kill_switch.is_set():
            return OrderReceipt(
                order_id=OrderId(value="rejected"),
                status=OrderStatus.REJECTED,
                message="kill_switch_active",
            )

        # Idempotency check
        if self._guard is not None and request.correlation_id is not None:
            dup = self._guard.check_and_reserve(request.correlation_id)
            if dup is not None:
                # v3 parity: return the original result
                return dup.result

        # Risk check
        if self._risk is not None and not self._risk.check(request):
            log.warning("Risk check failed for order")
            order = self._make_order(request, OrderStatus.REJECTED)
            self._order_manager.on_order_created(order)
            self._bus.publish(OrderRejected(order=order, reason="risk_check_failed"))
            if self._metrics is not None:
                self._metrics.counter("orders.rejected").inc()
                self._metrics.counter("risk.rejected").inc()
            return OrderReceipt(
                order_id=order.order_id,
                status=OrderStatus.REJECTED,
                message="risk_check_failed",
            )

        # Fill
        try:
            order, fill = self._fill.submit(request)
        except Exception as exc:
            boundary_crossed = getattr(self._fill, "submission_boundary_crossed", False)
            if boundary_crossed:
                # Don't release idempotency key — order may be at broker
                from tradex_domain.errors import OrderSubmissionUnknownError
                raise OrderSubmissionUnknownError(
                    f"Order submission failed after crossing broker boundary: {exc}"
                ) from exc
            if self._guard is not None and request.correlation_id is not None:
                self._guard.release(request.correlation_id)
            order = self._make_order(request, OrderStatus.REJECTED)
            self._order_manager.on_order_created(order)
            self._bus.publish(OrderRejected(order=order, reason=str(exc)))
            if self._metrics is not None:
                self._metrics.counter("orders.rejected").inc()
            return OrderReceipt(
                order_id=order.order_id,
                status=OrderStatus.REJECTED,
                message=str(exc),
            )

        # OMS update
        self._order_manager.on_order_created(order)
        self._bus.publish(OrderPlaced(order=order))

        if fill is not None:
            self._order_manager.on_order_filled(order, fill)
            self._position_manager.on_fill(fill)
            self._bus.publish(OrderFilled(fill=fill))
            log.info(
                "Order filled: %s qty=%s price=%s",
                order.order_id, fill.quantity, fill.price,
            )
            if self._guard is not None and request.correlation_id is not None:
                self._guard.record_result(request.correlation_id, order.order_id)
            if self._metrics is not None:
                self._metrics.counter("orders.submitted").inc()
                self._metrics.counter("orders.filled").inc()
        else:
            if self._metrics is not None:
                self._metrics.counter("orders.submitted").inc()

        return OrderReceipt(
            order_id=order.order_id,
            status=order.status,
            message="submitted",
        )

    def trip_kill_switch(self, reason: str = "") -> list[str]:
        """Halt new submissions and cancel every open order."""
        log.critical("Kill switch tripped: %s", reason)
        if self._metrics is not None:
            self._metrics.counter("kill_switch.tripped").inc()
        self._kill_switch.set()
        # Propagate to risk manager master gate
        if self._risk is not None:
            self._risk.live_orders_enabled = False
        failures: list[str] = []
        for order in self._cache.all_orders():
            if order.status not in _TERMINAL_STATUSES:
                try:
                    self.cancel(order.order_id)
                    # Also cancel at broker via fill source
                    if hasattr(self._fill, "cancel"):
                        self._fill.cancel(order.order_id)
                except Exception as exc:
                    log.error("kill-switch cancel failed for %s: %s", order.order_id, exc)
                    failures.append(order.order_id.value)
        return failures

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
        return drifts

    def cancel(self, order_id: OrderId) -> Order:
        """Cancel an order."""
        log.info("Cancelling order %s", order_id)
        order = self._cache.get_order(order_id.value)
        if order is None:
            raise OrderRejectedError(f"Order {order_id.value} not found")
        cancelled = order.transition_to(OrderStatus.CANCELLED)
        self._cache.update_order(cancelled)
        return cancelled

    @property
    def kill_switch(self) -> bool:
        """Whether the kill switch is active."""
        return self._kill_switch.is_set()

    @kill_switch.setter
    def kill_switch(self, value: bool) -> None:
        """Activate or deactivate the kill switch."""
        if value:
            self._kill_switch.set()
        else:
            self._kill_switch.clear()

    def get_order(self, order_id: OrderId) -> Order | None:
        """Look up an order by ID."""
        return self._cache.get_order(order_id.value)

    def all_orders(self) -> list[Order]:
        """Return all cached orders."""
        return self._cache.all_orders()

    @property
    def cache(self) -> TradingCache:
        """Access the trading cache."""
        return self._cache


__all__ = [
    "ExecutionEngine",
    "IdempotencyDuplicate",
    "IdempotencyGuard",
    "InMemoryOrderStore",
    "MemoryIdempotencyGuard",
    "OrderStore",
    "RiskCheckResult",
    "RiskManager",
]
