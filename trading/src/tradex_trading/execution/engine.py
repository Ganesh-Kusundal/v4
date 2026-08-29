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
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from datetime import UTC, datetime
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
    ErrorOccurred,
    OrderCancelled,
    OrderFilled,
    OrderModified,
    OrderPlaced,
    OrderRejected,
    PlaceOrderCommand,
)
from tradex_domain.execution import Fill, Order, OrderReceipt, OrderRequest, Position
from tradex_domain.value_objects import CorrelationId, OrderId

from tradex_trading.execution.fees import FeeCalculator
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


@dataclass(frozen=True, slots=True)
class RiskBudget:
    """Per-strategy slice of the broker-level risk envelope (G2).

    Each strategy on the same account draws from its own budget so a
    buggy strategy on instrument A can never exhaust the limit for
    strategy B on instrument C. Strategies with no budget (legacy /
    ad-hoc orders) fall back to the global caps on ``RiskManager``.
    """

    strategy_id: str
    max_order_value: Decimal | None = None
    max_position_value: Decimal | None = None
    max_daily_loss_amt: Decimal | None = None
    max_drawdown_pct: Decimal | None = None


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
        positions_provider: Any | None = None,
        price_provider: Any | None = None,
        reject_unknown_market_value: bool = False,
        max_daily_loss_amt: Decimal | None = None,
        max_drawdown_pct: Decimal | None = None,
        budgets: dict[str, "RiskBudget"] | None = None,
    ) -> None:
        self._max_order_value = max_order_value
        self._max_position_value = max_position_value
        self._max_orders_per_minute = max_orders_per_minute
        self._recent_orders: deque[datetime] = deque()
        self._lock = threading.Lock()
        self._live_orders_enabled = live_orders_enabled
        #: When True, a MARKET order whose price/mark cannot be resolved is
        #: denied (fail-closed for live); when False it preserves the legacy
        #: behaviour of skipping the order-value gate (dev/paper).
        self._reject_unknown_market_value = reject_unknown_market_value
        #: Daily-loss and drawdown limits. Guards only get tighter as the day
        #: progresses; they always allow reductions/flattening.
        self._max_daily_loss_amt = max_daily_loss_amt
        self._max_drawdown_pct = max_drawdown_pct
        self._session_date: Any = None
        self._base_pnl = Decimal("0")
        self._peak_pnl = Decimal("0")
        #: Callable returning current positions (e.g. an OMS cache) so
        #: ``max_position_value`` can be enforced against live exposure. When
        #: None (backtest boot, unit tests), the position check is skipped.
        self._positions_provider = positions_provider
        #: Callable mapping an instrument to its current market price (Price
        #: or Decimal). When None (or when it yields no usable price),
        #: exposure falls back to the position's avg_price.
        self._price_provider = price_provider
        #: G2: per-strategy risk budgets. ``check()`` resolves the active
        #: budget from ``request.tag`` (the strategy_id; the strategy engine
        #: already stamps ``strategy_id@version`` into tag). A request with
        #: no matching budget falls back to the global caps above.
        self._budgets: dict[str, RiskBudget] = dict(budgets or {})
        #: Count of orders denied by ``check()`` (any gate). Read by
        #: BacktestEngine to populate ``BacktestResult.num_rejected`` without
        #: re-implementing rejection bookkeeping in its own loop.
        self._rejected_count = 0

    def _active_budget(self, request: Any) -> tuple[Decimal | None, ...]:
        """Resolve the active cap values for ``request``.

        Returns ``(max_order_value, max_position_value,
        max_daily_loss_amt, max_drawdown_pct)`` for the strategy, or
        the global caps if the strategy has no budget. G2.
        """
        sid = (request.tag or "").strip() if request is not None else ""
        budget = self._budgets.get(sid) if sid else None
        if budget is not None:
            return (
                budget.max_order_value,
                budget.max_position_value,
                budget.max_daily_loss_amt,
                budget.max_drawdown_pct,
            )
        return (
            self._max_order_value,
            self._max_position_value,
            self._max_daily_loss_amt,
            self._max_drawdown_pct,
        )

    def _position_exposure(self) -> Decimal:
        """Absolute notional of all open positions (qty * market or avg price).

        Prefers the current market price via the bound ``price_provider``
        when available; falls back to the position's avg_price otherwise.
        """
        total = Decimal("0")
        if self._positions_provider is None:
            return total
        positions = self._positions_provider()
        for pos in positions:
            qty = getattr(pos, "quantity", None)
            avg = getattr(pos, "avg_price", None)
            if qty is None or avg is None:
                continue
            mark = self._mark_price(getattr(pos, "instrument", None))
            total += abs(qty.value) * (mark if mark is not None else avg.value)
        return total

    def _mark_price(self, instrument: Any) -> Decimal | None:
        """Current market price for an instrument, or None if unavailable."""
        if self._price_provider is None or instrument is None:
            return None
        try:
            quote = self._price_provider(instrument)
        except Exception:
            return None
        if quote is None:
            return None
        value = getattr(quote, "value", quote)
        try:
            value = Decimal(str(value))
        except Exception:
            return None
        return value if value > 0 else None

    def _mark_for_trade(self, request: OrderRequest) -> Decimal | None:
        """Effective price for notional checks: the request price, else the
        current market mark. ``None`` when neither is resolvable.

        MARKET orders often carry no price; without this, their notional
        collapses to zero and the order-value gate is silently bypassed.
        """
        price = request.price
        if price is not None and price.value > 0:
            return price.value
        return self._mark_price(request.instrument)

    def _incoming_exposure(self, request: OrderRequest) -> Decimal:
        """Notional of the incoming order (price * quantity)."""
        mark = self._mark_for_trade(request)
        if mark is None:
            return Decimal("0")
        return mark * request.quantity.value

    @property
    def live_orders_enabled(self) -> bool:
        """Master gate: when False, all orders are rejected."""
        return self._live_orders_enabled

    @live_orders_enabled.setter
    def live_orders_enabled(self, value: bool) -> None:
        self._live_orders_enabled = value

    def set_positions_provider(self, provider: Any) -> None:
        """Bind the position source used for ``max_position_value``.

        ``provider`` is a zero-arg callable returning an iterable of
        positions (e.g. ``engine.cache.all_positions``). When unset the
        position check is skipped.
        """
        self._positions_provider = provider

    def set_price_provider(self, provider: Any) -> None:
        """Bind a market-price source for exposure marking.

        ``provider`` is a one-arg callable mapping an instrument to its
        current price (``Price`` or ``Decimal``). When unset — or when it
        returns nothing usable — exposure falls back to avg_price.
        """
        self._price_provider = provider

    def bind_cash_provider(self, provider: Any) -> None:
        """Bind a zero-arg callable returning available cash (Decimal).

        When bound, every BUY in :meth:`check` is rejected if its
        incoming notional exceeds the returned cash. SELLs are never
        cash-gated (a sell is a credit, not a debit). When unset, the
        cash gate is skipped — backward-compatible with risk configs that
        do not track cash.
        """
        self._cash_provider = provider

    @property
    def cash_provider_bound(self) -> bool:
        """True when a cash provider is bound (C1 cash gate live)."""
        return getattr(self, "_cash_provider", None) is not None

    @property
    def positions_provider_bound(self) -> bool:
        """True when a positions provider is bound (max_position_value live)."""
        return self._positions_provider is not None

    def check(self, request: OrderRequest, now: datetime | None = None) -> bool:
        """Return True if the order passes risk checks, False to reject.

        Parameters
        ----------
        now : datetime | None
            Evaluation instant for the orders-per-minute window. Defaults to
            wall clock (``datetime.now(UTC)``). A caller replaying a
            deterministic event stream — e.g. BacktestEngine — should pass the
            event timestamp so the rate-limit decision reproduces across runs
            (parity review area #5: deterministic event processing).

        Notes
        -----
        All ``now`` values within one manager must share tz-awareness
        (naive or aware) — the window subtraction compares them directly.
        BacktestEngine resets the window at run start, so one manager is
        dedicated to one mode and never mixes IST tz-naive backtest
        timestamps with aware wall-clock live timestamps.
        """
        with self._lock:
            # G2: resolve the per-strategy (or global) cap values once
            # at the top. Every gate below reads from this tuple, so a
            # request with a matching budget sees its slice, and a
            # request with no budget falls back to the global caps.
            (
                max_order_value,
                max_position_value,
                max_daily_loss_amt,
                max_drawdown_pct,
            ) = self._active_budget(request)

            # C4: defensive tz check. Mixing tz-aware and tz-naive datetimes
            # in the rate-limit window subtraction raises TypeError deep
            # in (now - self._recent_orders[0]).total_seconds(). Validate
            # the awareness of `now` against the window's existing
            # awareness (the first entry in self._recent_orders). When
            # the window is empty, accept whatever awareness the caller
            # passes; a fresh manager has no history to mismatch.
            if now is not None and self._recent_orders:
                if (now.tzinfo is None) != (self._recent_orders[0].tzinfo is None):
                    raise ValueError(
                        "RiskManager.check: tz-awareness mismatch in rate-limit "
                        f"window. now.tzinfo={now.tzinfo!r} but existing entries "
                        f"are "
                        f"{'aware' if self._recent_orders[0].tzinfo else 'naive'}. "
                        "All calls in one session must share tz-awareness "
                        "(see docstring). BacktestEngine passes naive datetimes; "
                        "live wall-clock is aware."
                    )

            # Master gate
            if not self._live_orders_enabled:
                return self._deny()

            # Order value check (per-strategy or global)
            if max_order_value is not None:
                mark = self._mark_for_trade(request)
                if mark is None:
                    if self._reject_unknown_market_value:
                        # Fail-closed live: don't let an unpriced MARKET order
                        # slip past the notional gate with no mark to bind it.
                        return self._deny()
                elif mark * request.quantity.value > max_order_value:
                    return self._deny()

            # Cash check (C1) — only BUY is cash-gated; a SELL is a credit.
            # Skipped when no cash provider is bound (backward-compat with
            # risk configs that do not track cash). Incoming notional is
            # the notional the order would add to existing exposure.
            if (
                request.side is OrderSide.BUY
                and getattr(self, "_cash_provider", None) is not None
            ):
                try:
                    cash = self._cash_provider()
                except Exception:
                    cash = None
                if cash is not None:
                    incoming = self._incoming_exposure(request)
                    if incoming > Decimal(str(cash)):
                        return self._deny()

            # Position value check (per-strategy or global)
            if max_position_value is not None:
                exposure = self._position_exposure() + self._incoming_exposure(request)
                if exposure > max_position_value:
                    return self._deny()

            # Daily-loss / drawdown guards (per-strategy or global).
            # Only deny new exposure, never a reduction.
            if (
                max_daily_loss_amt is not None
                or max_drawdown_pct is not None
            ) and self._positions_provider is not None:
                net = self._session_net(now)
                if max_daily_loss_amt is not None and (
                    net <= -max_daily_loss_amt
                ) and self._increases_exposure(request):
                    return self._deny()
                if max_drawdown_pct is not None:
                    dd = self._drawdown(net)
                    if dd is not None and dd >= max_drawdown_pct \
                            and self._increases_exposure(request):
                        return self._deny()

            # Rate limit check
            if self._max_orders_per_minute is not None:
                now = now if now is not None else datetime.now(UTC)
                # Purge old entries
                while self._recent_orders and (now - self._recent_orders[0]).total_seconds() > 60:
                    self._recent_orders.popleft()
                if len(self._recent_orders) >= self._max_orders_per_minute:
                    return self._deny()
                self._recent_orders.append(now)

            return True

    def _deny(self) -> bool:
        """Record a rejection and return ``False`` (caller returns it)."""
        self._rejected_count += 1
        return False

    # -- session PnL for daily-loss / drawdown guards ---------------------

    def _equity_pnl(self) -> Decimal:
        """Portfolio realized + unrealized PnL from the position book.

        ``0`` when no position provider is bound (guards are then inert).
        """
        if self._positions_provider is None:
            return Decimal("0")
        total = Decimal("0")
        for pos in self._positions_provider():
            total += pos.realized_pnl.amount + pos.unrealized_pnl.amount
        return total

    def _session_net(self, now: datetime | None) -> Decimal:
        """Today's PnL relative to the day-start baseline, maintaining the
        day's running peak. Rolls the baseline forward on a date change so
        yesterday's losses never leak into today's limit.
        """
        today = (now if now is not None else datetime.now(UTC)).date()
        if self._session_date != today:
            self._session_date = today
            self._base_pnl = self._equity_pnl()
            self._peak_pnl = Decimal("0")
        net = self._equity_pnl() - self._base_pnl
        if net > self._peak_pnl:
            self._peak_pnl = net
        return net

    def _drawdown(self, net: Decimal) -> Decimal | None:
        """Fractional drawdown from the session peak, or ``None`` at/below 0."""
        if self._peak_pnl <= 0:
            return None
        return (self._peak_pnl - net) / self._peak_pnl

    def _increases_exposure(self, request: OrderRequest) -> bool:
        """True when the order grows the existing position (or opens a new one),
        as opposed to reducing/flattening it.

        Reduction is always allowed so a breached daily limit never *locks* a
        position onto the book — exits stay free.
        """
        signed = (
            request.quantity.value
            if request.side is OrderSide.BUY
            else -request.quantity.value
        )
        if self._positions_provider is None:
            return False
        for pos in self._positions_provider():
            if pos.instrument == request.instrument:
                existing = pos.quantity.value
                new_signed = existing + signed
                return new_signed * new_signed > existing * existing
        # No existing position -> the order is a fresh open / increase.
        return True

    @property
    def rejected_count(self) -> int:
        """Number of orders denied by :meth:`check` since construction."""
        return self._rejected_count

    def reset_rate_window(self) -> None:
        """Clear the orders-per-minute window and rejection counter.

        BacktestEngine calls this at the start of every run so a shared
        RiskManager never leaks rate-limit state between independent
        backtests — results stay a pure function of (data, config,
        strategy) (parity review area #8: reproducible). The reactive/live
        path never calls it, so the live rate limit is unaffected.
        """
        with self._lock:
            self._recent_orders.clear()
            self._rejected_count = 0

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
        fee_calculator: FeeCalculator | None = None,
        applied_fills_max: int = 50_000,
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
        self._fee_calculator = fee_calculator
        self._order_manager = OrderManager(self._cache)
        self._position_manager = PositionManager(self._cache)
        self._kill_switch = threading.Event()
        self._reconciler = ReconciliationEngine()
        self._brokerage_accrued: dict[str, Decimal] = {}
        self._brokerage_lock = threading.Lock()  # ponytail: unbounded, LRU 50k if needed
        #: Fingerprints of OrderFilled events already applied to the OMS
        #: (order_id + side + qty + price) — re-published broker fills are
        #: skipped, distinct partial fills are each applied in full.
        #:
        #: **Capacity:** bounded at ``applied_fills_max`` (default 50_000) as
        #: an LRU; oldest fingerprint is evicted when the cap is exceeded.
        #: Eviction is observable via the ``bus.applied_fills.evicted`` counter
        #: so operators can detect under-provisioned dedup windows.
        self._applied_fills: OrderedDict[tuple, None] = OrderedDict()
        self._applied_fills_max = applied_fills_max
        self._applied_fills_lock = threading.Lock()
        #: M2: side-table mapping each order_id to the CorrelationId reserved
        #: for it in the pipeline. Populated when ``check_and_reserve`` returns
        #: ``None`` (cid is fresh); consulted by ``cancel()`` so the cid is
        #: released when the order is cancelled (the pipeline normally records
        #: the result for FILLED orders, but a cancellation never reaches that
        #: path). Cleared from the table once the cid is released so the
        #: mapping never leaks between orders.
        self._cid_for_order: dict[OrderId, CorrelationId] = {}
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

        # Inbound live-fill bridge — broker order streams (or any publisher)
        # publish OrderFilled on the bus; the engine applies the fill to the
        # OMS idempotently. This is what makes live fills reach the position
        # manager (BrokerFillSource returns ACK-with-no-fill synchronously).
        self._fill_disposable = self._bus.of_type(OrderFilled).subscribe(
            on_next=self._apply_fill,
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
        if self._kill_switch.is_set():
            if sync:
                return OrderReceipt(
                    order_id=OrderId(value="rejected"),
                    status=OrderStatus.REJECTED,
                    message="kill_switch_active",
                )
            return None

        # 1. Idempotency check
        cid = request.correlation_id
        if self._guard is not None and cid is not None:
            dup = self._guard.check_and_reserve(cid)
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

        # 2. Risk check
        if self._risk is not None and not self._risk.check(request):
            log.warning("Risk check failed for order")
            order = self._make_order(request, OrderStatus.REJECTED)
            self._order_manager.on_order_created(order)
            self._bus.publish(OrderRejected(order=order, reason="risk_check_failed"))
            if self._metrics is not None:
                self._metrics.counter("orders.rejected").inc()
                self._metrics.counter("risk.rejected").inc()
            # M2: release the reservation on risk-reject so the cid is
            # immediately reusable. Risk rejection is a terminal state for
            # the request — the order never reaches the cancel path and
            # would otherwise leak the cid forever.
            if self._guard is not None and cid is not None:
                self._guard.release(cid)
            return (
                OrderReceipt(
                    order_id=order.order_id,
                    status=OrderStatus.REJECTED,
                    message="risk_check_failed",
                )
                if sync
                else None
            )

        # 3. Fill
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
            if self._guard is not None and cid is not None:
                self._guard.release(cid)
            order = self._make_order(request, OrderStatus.REJECTED)
            self._order_manager.on_order_created(order)
            self._bus.publish(OrderRejected(order=order, reason=str(exc)))
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
        # M2: stamp the order_id → cid mapping so cancel() can release
        # the reservation. The order has just been created; from here
        # forward the cid is owned by this order. record_result() below
        # removes the entry from the reserved set but does not clear the
        # side-table — cancel() needs the cid even after record_result.
        if self._guard is not None and cid is not None:
            self._cid_for_order[order.order_id] = cid
        self._bus.publish(OrderPlaced(order=order))

        if fill is not None:
            # Guard: skip position update if fill source owns projection
            # (PaperBroker projects positions itself). Idempotent against
            # _apply_fill: the order is FILLED before OrderFilled is published.
            if not getattr(self._fill, "position_projection_owned", False):
                self._position_manager.on_fill(fill)
                self._apply_fee(fill)
            self._order_manager.on_order_filled(order, fill)
            # G3: record the fingerprint in the applied-fills set BEFORE
            # publishing so that any re-publish from the live-fill bridge
            # (which subscribes to OrderFilled) is short-circuited by
            # the dedup check in _apply_fill.
            self._record_applied_fill(fill)
            self._bus.publish(OrderFilled(fill=fill))
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

    def _apply_fee(self, fill: Fill) -> None:
        """Deduct *fill*'s fees from the position's realized P&L when fees are
        enabled. No-op when no fee calculator is bound or no position exists.
        Failures propagate loudly — a silently-swallowed fee bug is exactly
        the accounting divergence the parity work exists to prevent.

        Brokerage ₹20 per-order cap is enforced across partial fills:
        each fill's brokerage is capped to the remaining headroom
        ``20 - accrued`` so an order with two 5-lot fills at 10k pays
        15 + 5 = 20, not 15 + 15 = 30 (H2).
        """
        if self._fee_calculator is None:
            return
        from tradex_domain.utils import q2
        from tradex_domain.value_objects import Money

        from tradex_trading.execution.fees import (
            _BROKERAGE_CAP,
            _GST_RATE,
            FeeCalculator,
        )

        # Canonical breakdown to isolate the per-fill brokerage.
        breakdown = FeeCalculator.equity_intraday(
            side=fill.side, price=fill.price.value, quantity=fill.quantity.value
        )
        calculated = breakdown.broker_fee
        oid = fill.order_id.value if hasattr(fill.order_id, "value") else str(fill.order_id)
        with self._brokerage_lock:
            accrued = self._brokerage_accrued.get(oid, Decimal("0"))
            remaining = _BROKERAGE_CAP - accrued
            if remaining < Decimal("0"):
                remaining = Decimal("0")
            capped = min(calculated, remaining)
            self._brokerage_accrued[oid] = accrued + capped
        if capped < calculated:
            # Recompute GST on the capped brokerage so total stays consistent.
            gst_new = q2(
                (capped + breakdown.exchange_fee + breakdown.sebi_fee) * _GST_RATE
            )
            total = (
                capped
                + breakdown.exchange_fee
                + breakdown.stt
                + breakdown.sebi_fee
                + breakdown.stamp_duty
                + gst_new
            )
            fee = Money(amount=q2(total))
        else:
            fee = self._fee_calculator.calculate(fill)
        if fee.amount > 0:
            self._position_manager.on_fee(fill, fee)

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

    def _record_applied_fill(self, fill: Any) -> bool:
        """Insert the fill's fingerprint into the applied-fills LRU.

        H3 contract:

        - The set is bounded at ``self._applied_fills_max`` (default
          ``50_000``, configurable via the ``applied_fills_max`` ctor arg).
        - On overflow, the **oldest** key is evicted (LRU end) and the
          ``bus.applied_fills.evicted`` counter increments by 1. Operators
          watch this counter to detect an under-provisioned dedup window.
        - A re-publish (key already present) moves the key to the MRU end
          and reports a duplicate (returns ``True``); the caller must skip
          the apply. A fresh key is inserted at the MRU end and the caller
          proceeds (returns ``False``).

        Returns True if the key was already present (re-publish — skip apply).
        Returns False if the key is new and was inserted (proceed to apply).
        """
        if fill.fill_id is not None:
            key: tuple = (fill.fill_id,)
        else:
            key = (
                fill.order_id.value, fill.side.value, str(fill.quantity.value),
                str(fill.price.value),
            )
        evicted = False
        with self._applied_fills_lock:
            if key in self._applied_fills:
                self._applied_fills.move_to_end(key)
                return True
            self._applied_fills[key] = None
            if len(self._applied_fills) > self._applied_fills_max:
                self._applied_fills.popitem(last=False)
                evicted = True
        if evicted and self._metrics is not None:
            # H3: each LRU eviction is counted so a chronically under-sized
            # dedup window is visible in the metrics without parsing logs.
            self._metrics.counter("bus.applied_fills.evicted").inc()
        return False

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
        if self._record_applied_fill(fill):
            # Re-published fingerprint (same fill_id, or same
            # (order, side, qty, price) when no fill_id). Skip
            # silently — this is the contract that protects the
            # synchronous pipeline path from double-apply via
            # the live-fill bridge, and protects the bridge from
            # its own re-publishes.
            return
        # _record_applied_fill inserted a new key above. Now safe to apply.

        existing = self._cache.get_order(fill.order_id.value)
        if existing is not None and existing.status == OrderStatus.REJECTED:
            # REJECTED stays rejected — do not position-update.
            return
        if not getattr(self._fill, "position_projection_owned", False):
            self._position_manager.on_fill(fill)
            self._apply_fee(fill)
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
        """Cancel an order and publish ``OrderCancelled`` on the bus.

        M2: also release the idempotency reservation that was made for
        this order in ``_run_pipeline``. Without this, every cancelled
        order whose cid was reserved leaked a slot in the guard's
        ``_reserved`` set. The reservation is looked up via the
        ``_cid_for_order`` side-table populated by the pipeline; an
        order that never had a cid reserved (e.g. risk-rejected) is
        not in the table and the lookup is a no-op.
        """
        log.info("Cancelling order %s", order_id)
        order = self._cache.get_order(order_id.value)
        if order is None:
            raise OrderRejectedError(f"Order {order_id.value} not found")
        cancelled = order.transition_to(OrderStatus.CANCELLED)
        self._cache.update_order(cancelled)
        self._bus.publish(OrderCancelled(order=cancelled))
        # M2: release the idempotency reservation that was made for
        # this order. release() is idempotent (discard on a missing
        # key), so this is safe even if the pipeline already released
        # the cid (e.g. on risk rejection before the side-table was
        # populated, or on a non-boundary fill-source error). Pop the
        # side-table so the mapping doesn't outlive the order.
        cid = self._cid_for_order.pop(order_id, None)
        if self._guard is not None and cid is not None:
            self._guard.release(cid)
        return cancelled

    def modify(self, order_id: OrderId, request: OrderRequest) -> Order:
        """Modify an open order — the third mutation on the canonical spine.

        The broker side is reached through the fill source (live sources
        forward to the venue; simulated/paper/replay are no-ops), then the
        modified fields are projected into the OMS so the cache and the
        venue agree — previously modifications went straight to the broker
        and silently desynced the engine cache.

        H5: also re-runs ``RiskManager.check()`` on the modified request.
        An order within limits at entry could be modified to exceed
        ``max_position_value``; without this guard the position can
        grow past the configured cap.
        """
        log.info("Modifying order %s", order_id)
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
        # H5: re-run risk on the modified request. Reject (and roll back
        # the cache) if the modified notional exceeds the configured cap.
        # The broker-side modify is called *before* this check because
        # simulated/paper/replay fill sources have a no-op modify that
        # must not raise; the check is the source of truth.
        modify_fn = getattr(self._fill, "modify", None)
        if callable(modify_fn):
            modify_fn(order_id, request)
        if self._risk is not None and not self._risk.check(request):
            # Roll back the cache to the pre-modify state.
            self._cache.update_order(order)
            raise OrderRejectedError(
                f"Order {order_id.value}: modified request rejected by risk check"
            )
        modified = replace(
            order,
            order_type=request.order_type,
            quantity=request.quantity,
            price=request.price,
            trigger_price=request.trigger_price,
            time_in_force=request.time_in_force,
        )
        self._cache.update_order(modified)
        self._bus.publish(OrderModified(order=modified))
        return modified

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
