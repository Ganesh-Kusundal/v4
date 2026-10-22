"""Slippage models for execution simulation.

Provides pluggable slippage models that can be applied to fill prices
to simulate realistic market impact in backtests and paper trading.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from tradex_domain.enums import OrderSide
from tradex_domain.value_objects import Price, Quantity


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
