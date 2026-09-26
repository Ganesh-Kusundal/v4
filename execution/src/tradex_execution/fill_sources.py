"""Fill source abstraction — the key seam between execution modes.

A FillSource is what turns an OrderRequest into an Order + optional Fill.
Different implementations serve backtesting, paper trading, live broker
execution, and historical replay. All price-resolving sources delegate to the
shared ``FillModel`` so identical input events fill identically in every mode
(cross-mode parity, HIGH-6b).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from tradex_domain.enums import OrderStatus
from tradex_domain.errors import OrderSubmissionUnknownError
from tradex_domain.execution import Fill, Order, OrderRequest
from tradex_domain.protocols import TradingCacheProtocol
from tradex_domain.value_objects import OrderId, Price

from tradex_execution.fill_model import FillModel


@runtime_checkable
class FillSource(Protocol):
    """Source of order fills — the key abstraction for execution modes."""

    def submit(self, request: OrderRequest) -> tuple[Order, Fill | None]: ...

    def cancel(self, order_id: OrderId) -> None: ...

    def modify(self, order_id: OrderId, request: OrderRequest) -> None:
        """Forward an order modification to the execution venue (live sources).
        Simulated/paper/replay sources are no-ops — their OMS projection is
        the authoritative state."""


def _make_order(
    request: OrderRequest,
    status: OrderStatus = OrderStatus.FILLED,
    order_id: OrderId | None = None,
) -> Order:
    """Create an Order from an OrderRequest.

    Uses *order_id* when provided (broker-assigned), otherwise generates a
    fresh UUID (paper/simulated paths). Raw string ids from broker adapters
    are wrapped in ``OrderId`` so the OMS always sees a value object.
    """
    if order_id is not None and not isinstance(order_id, OrderId):
        raw = str(order_id)
        # M6: reject empty / whitespace-only ids that would otherwise
        # propagate to the OMS cache and break every later lookup
        # that joins on order_id.
        if not raw.strip():
            raise ValueError(
                "BrokerFillSource: broker returned an empty order_id; "
                "cannot build an Order with a blank id"
            )
        order_id = OrderId(value=raw)
    if order_id is None:
        order_id = OrderId(value=str(uuid.uuid4()))
    return Order(
        order_id=order_id,
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
        # A bracket (super) order must retain its protective legs in the OMS
        # so later lifecycle mutations (cancel/modify) can be dispatched to
        # the venue's super-order endpoints instead of the plain ones.
        target_price=request.target_price,
        stop_loss_price=request.stop_loss_price,
        trailing_jump=request.trailing_jump,
    )


class SimulatedFillSource(FillModel):
    """Backtest fill source — immediate fill at requested price.

    If no price is specified (MARKET order), uses the trigger_price or
    falls back to a zero price (caller should set explicit prices).

    Parameters
    ----------
    portfolio_state:
        Optional live portfolio for position-constraint checks.
    slippage_model:
        Optional slippage model applied to the fill price.
    """

    def __init__(
        self,
        portfolio_state: object | None = None,
        slippage_model: object | None = None,
        *,
        clock: object | None = None,
        require_reference_timestamp: bool = False,
    ) -> None:
        super().__init__(
            slippage_model=slippage_model,
            clock=clock,
            require_reference_timestamp=require_reference_timestamp,
        )
        self._portfolio_state = portfolio_state
        #: Number of times the venue was asked to fill. Tests assert the engine
        #: did NOT cross the broker boundary, which a mock return value cannot show.
        self.submit_calls = 0

    def submit(self, request: OrderRequest) -> tuple[Order, Fill | None]:
        self.submit_calls += 1
        order = _make_order(request, status=OrderStatus.FILLED)

        # Shared FillModel: request/trigger price -> positive-price guard ->
        # slippage. A zero-priced fill silently corrupts every downstream P&L
        # number (avg_price=0) and breaks FeeCalculator — fail loudly so the
        # caller prices the order (strategy bridge now stamps reference prices;
        # direct callers must pass price/trigger_price too).
        fill_price = self.resolve_fill_price(request)

        # If portfolio state available, check position constraints
        if self._portfolio_state is not None and hasattr(
            self._portfolio_state, "get_position"
        ):
            self._portfolio_state.get_position(request.instrument)
            # Could add position limit checks here

        fill = self.make_fill(order, fill_price, self.fill_timestamp(request))

        return order, fill

    def cancel(self, order_id: OrderId) -> None:
        """No-op for simulated fills."""

    def modify(self, order_id: OrderId, request: OrderRequest) -> None:
        """No-op for simulated fills — backtest orders fill immediately."""


class PaperFillSource(FillModel):
    """Paper fill source — immediate fill at latest quote or request price.

    If a quote is available in the cache, uses LTP; otherwise falls back
    to the request price. For MARKET orders without a price, uses a
    nominal value. Mirrors ``SimulatedFillSource``: optional slippage and
    deterministic fill timestamps from the request's reference timestamp.

    **Intentional divergence from PaperBroker** (GAP-3):
    ``PaperFillSource`` fills at LTP from the reactive cache (used by the
    ``ExecutionEngine`` pipeline). ``PaperBroker`` fills at LTP, limit
    price, or by walking a multi-level order book (used as a standalone
    ``BrokerAdapter``). Both converge for MARKET orders with seeded LTP
    quotes — the common case proven by
    ``test_paper_fill_convergence.py``. The divergence is intentional:
    ``PaperFillSource`` participates in the unified FillModel price
    resolution (shared with backtest/replay), while ``PaperBroker``
    simulates broker-specific order-book mechanics.
    """

    def __init__(
        self,
        cache: object | None = None,
        slippage_model: object | None = None,
        *,
        clock: object | None = None,
        require_reference_timestamp: bool = False,
    ) -> None:
        super().__init__(
            slippage_model=slippage_model,
            clock=clock,
            require_reference_timestamp=require_reference_timestamp,
        )
        self._cache = cache

    def bind_cache(self, cache: object) -> None:
        """Attach the OMS cache after engine construction.

        The composition root creates the fill source before the execution
        engine (which owns the cache), so paper mode binds the shared cache
        through this declared seam once the engine exists.
        """
        self._cache = cache

    def submit(self, request: OrderRequest) -> tuple[Order, Fill | None]:
        order = _make_order(request, status=OrderStatus.FILLED)

        # Paper-specific: prefer the LTP from the cache quote (mode-specific
        # market reference). Resolved through the shared FillModel so slippage
        # and the zero-price guard match every other mode.
        ltp = self._ltp_from_cache(request)
        if ltp is not None:
            fill_price = self.resolve_fill_price(request, market_price=ltp)
        elif request.price is not None:
            fill_price = self.resolve_fill_price(request)
        else:
            raise ValueError(
                f"cannot fill {request.instrument} ({request.side.value}) "
                f"without a positive price"
            )

        fill = self.make_fill(order, fill_price, self.fill_timestamp(request))
        return order, fill

    def _ltp_from_cache(self, request: OrderRequest) -> Price | None:
        """Latest traded price from the cache quote, if any."""
        if self._cache is None or not hasattr(self._cache, "get_quote"):
            return None
        quote = self._cache.get_quote(request.instrument)
        if quote is None:
            return None
        return Price(value=quote.ltp.value)

    def cancel(self, order_id: OrderId) -> None:
        """No-op for paper fills."""

    def modify(self, order_id: OrderId, request: OrderRequest) -> None:
        """No-op for paper fills — the OMS projection IS the paper state."""


class BrokerFillSource(FillModel):
    """Live fill source — delegates to broker adapter.

    Exposes boundary/projection metadata so the execution engine can
    make safe idempotency and position-management decisions.
    """

    def __init__(self, broker: object) -> None:
        super().__init__()
        self._broker = broker
        self._submission_boundary_crossed = False

    @property
    def submission_boundary_crossed(self) -> bool:
        """Whether the current submit attempt crossed into the broker adapter."""
        return self._submission_boundary_crossed

    @property
    def position_projection_owned(self) -> bool:
        """Whether the adapter already applies fills to its own OMS projection."""
        return bool(getattr(self._broker, "owns_position_projection", False))

    @property
    def position_projection_cache(self) -> TradingCacheProtocol | None:
        """Return the adapter's projection cache when it exposes one."""
        return getattr(self._broker, "trading_cache", None)

    def submit(self, request: OrderRequest) -> tuple[Order, Fill | None]:
        from tradex_domain.execution import BracketOrderRequest

        # The flag describes *this* attempt, so it is cleared here: it is a
        # one-way latch set only after a call is actually dispatched into the
        # adapter, and ExecutionEngine reads it when submit raises, to choose
        # between "rejected, safe to retry" and "outcome unknown, trip the
        # kill switch". Leaving a previous attempt's True in place made every
        # later failure misread as unknown-outcome.
        self._submission_boundary_crossed = False

        if isinstance(request, BracketOrderRequest):
            # A bracket is a *composite* submission: entry + protective legs
            # are placed together by the venue's super-order endpoint. The
            # adapter's capability gate is the backstop (the route preflights
            # the capability before reaching the engine), and the returned
            # id is the venue's super-order id — the OMS records it as the
            # entry order so dedup/replay and later lifecycle events join.
            submit = getattr(self._broker, "submit_super_order", None)
            if submit is None:
                raise ValueError("broker does not support super orders")
            broker_order_id = self._dispatch(submit, request)
            order = _make_order(request, status=OrderStatus.ACK, order_id=broker_order_id)
            return order, None
        # Delegate to broker adapter's submit_order method
        if hasattr(self._broker, "submit_order"):
            broker_order_id = self._dispatch(self._broker.submit_order, request)
            order = _make_order(request, status=OrderStatus.ACK, order_id=broker_order_id)
            return order, None
        # Fallback: create a pending order (fill will come via WebSocket)
        order = _make_order(request, status=OrderStatus.ACK)
        return order, None

    def _dispatch(self, send: Any, request: OrderRequest) -> Any:
        """Invoke *send* and record whether the attempt reached the venue.

        The latch is a best-effort signal for third-party fill sources; the
        authoritative one is the typed ``OrderSubmissionUnknownError`` that
        ``ProviderClient.submit_mutation`` raises for a transport failure after
        send or a 5xx. It is deliberately NOT set for a failure the adapter
        raises before anything is transmitted: entering the adapter is not the
        same as reaching the venue, and conflating the two used to trip the
        kill switch on a plain local validation error.
        """
        self._submission_boundary_crossed = True
        try:
            return send(request)
        except OrderSubmissionUnknownError:
            # Authoritative: the send failed *after* the request left.
            self._submission_boundary_crossed = True
            raise
        except Exception:
            # Definitive failure. Clear the latch so the engine treats the
            # order as rejected and retryable rather than unknown.
            self._submission_boundary_crossed = False
            raise

    def cancel(self, order_id: OrderId) -> None:
        """Cancel order at broker."""
        if hasattr(self._broker, "cancel_order"):
            self._broker.cancel_order(order_id)

    def cancel_super_order(self, order_id: OrderId) -> None:
        """Cancel a bracket (super) order at the broker.

        The venue cancels the whole composite — entry plus protective legs —
        via its super-order endpoint (``leg="ENTRY"`` default on the broker
        adapters). Reaching this method means the order is a bracket; a broker
        without super-order cancellation must fail loudly, never silently
        fall back to a plain cancel on the composite id.
        """
        if hasattr(self._broker, "cancel_super_order"):
            self._broker.cancel_super_order(order_id)
        else:
            from tradex_domain.errors import OrderRejectedError
            raise OrderRejectedError(
                "broker does not support super-order cancellation"
            )

    def modify(self, order_id: OrderId, request: OrderRequest) -> None:
        """Modify order at broker.

        A bracket modification carries the full composite (entry + protective
        legs) and must reach ``modify_super_order`` — a bracket sent through
        the plain modify endpoint would lose its legs. The engine refuses
        plain requests on bracket orders before this dispatch runs.
        """
        from tradex_domain.execution import BracketOrderRequest

        if isinstance(request, BracketOrderRequest):
            if hasattr(self._broker, "modify_super_order"):
                self._broker.modify_super_order(order_id, request)
            else:
                from tradex_domain.errors import OrderRejectedError
                raise OrderRejectedError(
                    "broker does not support super orders"
                )
            return
        if hasattr(self._broker, "modify_order"):
            self._broker.modify_order(order_id, request)


class ReplayFillSource(FillModel):
    """Replay fill source — replays historical fills in sequence.

    Fees recorded on the source fills are replayed as charged rather than
    recomputed. The brokerage cap accrues across a partial-fill sequence, so
    recomputing from a rate table yields a different number than the one
    actually charged, and replayed history would then disagree with the run it
    is replaying.
    """

    def __init__(
        self,
        fills: list[Fill],
        fees: list[Decimal | None] | None = None,
    ) -> None:
        super().__init__()
        self._fills = list(fills)
        self._fees = list(fees) if fees is not None else None
        self._index = 0
        self._issued: dict[int, Fill] = {}

    @property
    def replays_recorded_fees(self) -> bool:
        """True when this source supplies recorded fees the engine must use."""
        return self._fees is not None

    def fee_for(self, index: int) -> Decimal | None:
        """The recorded fee for fill *index*, or None when not recorded."""
        if self._fees is None or index >= len(self._fees):
            return None
        return self._fees[index]

    def submit(self, request: OrderRequest) -> tuple[Order, Fill | None]:
        index = self._index
        if index >= len(self._fills):
            # No more historical fills — return order without fill
            order = _make_order(request, status=OrderStatus.ACK)
            return order, None

        fill = self._fills[index]
        self._index += 1
        order = _make_order(request, status=OrderStatus.FILLED)
        # Replay-specific: preserve the recorded fill but re-stamp its
        # order_id to match the freshly minted order (via the shared FillModel).
        fill = self.restamp_fill(fill, order.order_id)
        # Remember what this hand-out cost so recorded_fee_for can answer for
        # the re-stamped object the engine now holds.
        self._issued[index] = fill
        return order, fill

    def recorded_fee_for(self, fill: Fill) -> Decimal | None:
        """The recorded fee for the fill *fill* was produced from.

        ``restamp_fill`` drops the venue trade id, so the re-stamped object is
        matched by identity against what ``submit`` last handed out.
        """
        if self._fees is None:
            return None
        for index, issued in self._issued.items():
            if issued is fill:
                return self.fee_for(index)
        return None

    def cancel(self, order_id: OrderId) -> None:
        """No-op for replay fills."""


    def modify(self, order_id: OrderId, request: OrderRequest) -> None:
        """No-op for replay fills."""


