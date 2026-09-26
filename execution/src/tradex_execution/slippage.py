"""Slippage models for execution simulation.

Provides pluggable slippage models that can be applied to fill prices
to simulate realistic market impact in backtests and paper trading.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill, Order, OrderRequest
from tradex_domain.value_objects import OrderId, Price, Quantity

if TYPE_CHECKING:
    from tradex_execution.fill_sources import FillSource


@runtime_checkable
class SlippageModel(Protocol):
    """Protocol for slippage models that adjust fill prices."""

    def apply(self, price: Price, side: OrderSide, quantity: Quantity) -> Price: ...


class NoSlippageModel:
    """No-op slippage model — returns the original price unchanged."""

    def apply(self, price: Price, side: OrderSide, quantity: Quantity) -> Price:
        return price


class FixedSlippageModel:
    """Fixed slippage model — applies a constant adjustment to the price.

    For BUY orders, slippage is added (worse fill).
    For SELL orders, slippage is subtracted (worse fill).
    """

    def __init__(self, constant: Decimal) -> None:
        self._constant = constant

    def apply(self, price: Price, side: OrderSide, quantity: Quantity) -> Price:
        adjustment = self._constant if side == OrderSide.BUY else -self._constant
        return Price(value=price.value + adjustment)


class PercentageSlippageModel:
    """Percentage-based slippage model — applies a percentage adjustment.

    For BUY orders, slippage is added (worse fill).
    For SELL orders, slippage is subtracted (worse fill).
    """

    def __init__(self, pct: Decimal) -> None:
        self._pct = pct

    def apply(self, price: Price, side: OrderSide, quantity: Quantity) -> Price:
        adjustment = price.value * self._pct
        if side == OrderSide.SELL:
            adjustment = -adjustment
        return Price(value=price.value + adjustment)


class SlippageAwareFillSource:
    """Wraps any FillSource, applying slippage to the fill price.

    This decorator pattern allows any fill source (simulated, paper, replay)
    to have realistic slippage applied without modifying the underlying source.
    """

    def __init__(self, inner: FillSource, slippage_model: SlippageModel) -> None:
        self._inner = inner
        self._slippage = slippage_model

    def submit(self, request: OrderRequest) -> tuple[Order, Fill | None]:
        order, fill = self._inner.submit(request)
        if fill is not None:
            adjusted_price = self._slippage.apply(fill.price, fill.side, fill.quantity)
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
        """Pass cancel through to the inner fill source (L2 fix — previously
        missing, so SlippageAwareFillSource didn't satisfy the FillSource
        protocol's cancel/modify methods)."""
        self._inner.cancel(order_id)

    def modify(self, order_id: OrderId, request: OrderRequest) -> None:
        """Pass modify through to the inner fill source."""
        self._inner.modify(order_id, request)


