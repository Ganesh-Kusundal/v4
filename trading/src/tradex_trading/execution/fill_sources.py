"""Fill source abstraction — the key seam between execution modes.

A FillSource is what turns an OrderRequest into an Order + optional Fill.
Different implementations serve backtesting, paper trading, live broker
execution, and historical replay.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from tradex_domain.enums import OrderStatus
from tradex_domain.execution import Fill, Order, OrderRequest
from tradex_domain.protocols import TradingCacheProtocol
from tradex_domain.value_objects import OrderId, Price


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
    fresh UUID (paper/simulated paths).
    """
    return Order(
        order_id=order_id if order_id is not None else OrderId(value=str(uuid.uuid4())),
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


class SimulatedFillSource:
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
        self._portfolio_state = portfolio_state
        self._slippage_model = slippage_model

    def submit(self, request: OrderRequest) -> tuple[Order, Fill | None]:
        order = _make_order(request, status=OrderStatus.FILLED)
        fill_price = request.price or request.trigger_price
        if fill_price is None or fill_price.value <= 0:
            # ponytail: a zero-priced fill silently corrupts every downstream
            # P&L number (avg_price=0) and breaks FeeCalculator. Fail loudly so
            # the caller prices the order (strategy bridge now stamps reference
            # prices; direct callers must pass price/trigger_price too).
            raise ValueError(
                f"SimulatedFillSource cannot fill {request.instrument} "
                f"({request.side.value}) without a positive price"
            )

        # If portfolio state available, check position constraints
        if self._portfolio_state is not None and hasattr(
            self._portfolio_state, "get_position"
        ):
            self._portfolio_state.get_position(request.instrument.symbol)
            # Could add position limit checks here

        fill = Fill(
            order_id=order.order_id,
            instrument=request.instrument,
            side=request.side,
            quantity=request.quantity,
            price=fill_price,
            # Deterministic: the order's reference market timestamp, or now().
            timestamp=request.reference_timestamp or datetime.now(UTC),
        )

        # Apply slippage model if provided
        if self._slippage_model is not None and hasattr(
            self._slippage_model, "apply"
        ):
            adjusted_price = self._slippage_model.apply(
                fill.price, fill.side, fill.quantity
            )
            fill = Fill(
                order_id=fill.order_id,
                instrument=fill.instrument,
                side=fill.side,
                quantity=fill.quantity,
                price=adjusted_price,
                timestamp=fill.timestamp,
            )

        return order, fill

    def cancel(self, order_id: OrderId) -> None:
        """No-op for simulated fills."""


class PaperFillSource:
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
        self._cache = cache
        self._slippage_model = slippage_model

    def submit(self, request: OrderRequest) -> tuple[Order, Fill | None]:
        order = _make_order(request, status=OrderStatus.FILLED)

        # Try to get price from cache quote
        fill_price_value = None
        if self._cache is not None and hasattr(self._cache, "get_quote"):
            quote = self._cache.get_quote(request.instrument.symbol)
            if quote is not None:
                fill_price_value = quote.ltp.value

        # Fallback to request price, then to a nominal value
        if fill_price_value is None:
            if request.price is not None:
                fill_price_value = request.price.value
            else:
                fill_price_value = Decimal("1.0")  # nominal paper price

        fill = Fill(
            order_id=order.order_id,
            instrument=request.instrument,
            side=request.side,
            quantity=request.quantity,
            price=Price(value=fill_price_value),
            timestamp=request.reference_timestamp or datetime.now(UTC),
        )

        # Apply slippage model if provided
        if self._slippage_model is not None and hasattr(
            self._slippage_model, "apply"
        ):
            adjusted_price = self._slippage_model.apply(
                fill.price, fill.side, fill.quantity
            )
            fill = Fill(
                order_id=fill.order_id,
                instrument=fill.instrument,
                side=fill.side,
                quantity=fill.quantity,
                price=adjusted_price,
                timestamp=fill.timestamp,
            )
        return order, fill

    def cancel(self, order_id: OrderId) -> None:
        """No-op for paper fills."""


class BrokerFillSource:
    """Live fill source — delegates to broker adapter.

    Exposes boundary/projection metadata so the execution engine can
    make safe idempotency and position-management decisions.
    """

    def __init__(self, broker: object) -> None:
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


class ReplayFillSource:
    """Replay fill source — replays historical fills in sequence."""

    def __init__(self, fills: list[Fill]) -> None:
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
        # Override fill's order_id to match the new order
        fill = Fill(
            order_id=order.order_id,
            instrument=fill.instrument,
            side=fill.side,
            quantity=fill.quantity,
            price=fill.price,
            timestamp=fill.timestamp,
        )
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
