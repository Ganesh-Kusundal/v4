"""Fill source abstraction — the key seam between execution modes.

A FillSource is what turns an OrderRequest into an Order + optional Fill.
Different implementations serve backtesting, paper trading, live broker
execution, and historical replay. All price-resolving sources delegate to the
shared ``FillModel`` so identical input events fill identically in every mode
(cross-mode parity, HIGH-6b).
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from tradex_domain.enums import OrderStatus
from tradex_domain.execution import Fill, Order, OrderRequest
from tradex_domain.protocols import TradingCacheProtocol
from tradex_domain.value_objects import OrderId, Price

from tradex_trading.execution.fill_model import FillModel


@runtime_checkable
class FillSource(Protocol):
    """Source of order fills — the key abstraction for execution modes."""

    def submit(self, request: OrderRequest) -> tuple[Order, Fill | None]: ...

    def cancel(self, order_id: OrderId) -> None: ...


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
        order_id = OrderId(value=str(order_id))
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
    ) -> None:
        super().__init__(slippage_model=slippage_model)
        self._portfolio_state = portfolio_state

    def submit(self, request: OrderRequest) -> tuple[Order, Fill | None]:
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


class PaperFillSource(FillModel):
    """Paper fill source — immediate fill at latest quote or request price.

    If a quote is available in the cache, uses LTP; otherwise falls back
    to the request price. For MARKET orders without a price, uses a
    nominal value. Mirrors ``SimulatedFillSource``: optional slippage and
    deterministic fill timestamps from the request's reference timestamp.
    """

    def __init__(
        self,
        cache: object | None = None,
        slippage_model: object | None = None,
    ) -> None:
        super().__init__(slippage_model=slippage_model)
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
        # Delegate to broker adapter's submit_order method
        if hasattr(self._broker, "submit_order"):
            self._submission_boundary_crossed = True
            broker_order_id = self._broker.submit_order(request)
            order = _make_order(request, status=OrderStatus.ACK, order_id=broker_order_id)
            return order, None
        # Fallback: create a pending order (fill will come via WebSocket)
        order = _make_order(request, status=OrderStatus.ACK)
        return order, None

    def cancel(self, order_id: OrderId) -> None:
        """Cancel order at broker."""
        if hasattr(self._broker, "cancel_order"):
            self._broker.cancel_order(order_id)


class ReplayFillSource(FillModel):
    """Replay fill source — replays historical fills in sequence."""

    def __init__(self, fills: list[Fill]) -> None:
        super().__init__()
        self._fills = list(fills)
        self._index = 0

    def submit(self, request: OrderRequest) -> tuple[Order, Fill | None]:
        if self._index >= len(self._fills):
            # No more historical fills — return order without fill
            order = _make_order(request, status=OrderStatus.ACK)
            return order, None

        fill = self._fills[self._index]
        self._index += 1
        order = _make_order(request, status=OrderStatus.FILLED)
        # Replay-specific: preserve the recorded fill but re-stamp its
        # order_id to match the freshly minted order (via the shared FillModel).
        fill = self.restamp_fill(fill, order.order_id)
        return order, fill

    def cancel(self, order_id: OrderId) -> None:
        """No-op for replay fills."""


__all__ = [
    "BrokerFillSource",
    "FillSource",
    "PaperFillSource",
    "ReplayFillSource",
    "SimulatedFillSource",
]
