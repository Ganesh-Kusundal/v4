"""Slippage models for execution simulation.

Provides pluggable slippage models that can be applied to fill prices
to simulate realistic market impact in backtests and paper trading.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

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


class MarketImpactSlippageModel:
    """Square-root market-impact slippage, aware of order size vs liquidity.

    A fixed-fraction model charges the same cost for a 1-lot order and a
    position that is a third of the day's volume. Real impact grows with
    *participation* — order size as a fraction of the volume that actually
    traded — and the square-root law is the standard first-order fit:

        impact_fraction = impact_coef * sqrt(quantity / adv)

    ``adv`` is the average (typically daily) volume for the instrument, looked
    up per request through *adv_provider* so a single model serves a whole
    universe with different liquidity. The fraction is capped at
    *max_fraction* so one order can never move the modelled price by more than
    that, and an unknown/zero ADV returns the price unchanged rather than
    inventing a division by zero.

    The model is **opt-in** and implements both the plain ``SlippageModel``
    protocol (using *default_adv*, for callers with no instrument context) and
    the richer ``apply_for_request`` the ``FillModel`` prefers when present, so
    instrument- and timestamp-aware ADV is used on the real path.
    """

    def __init__(
        self,
        *,
        adv_provider: Callable[[Any, datetime | None], Decimal] | None = None,
        impact_coef: Decimal | float | str = Decimal("0.1"),
        max_fraction: Decimal | float | str = Decimal("0.05"),
        default_adv: Decimal | float | str | None = None,
    ) -> None:
        self._adv_provider = adv_provider
        self._impact_coef = Decimal(str(impact_coef))
        self._max_fraction = Decimal(str(max_fraction))
        self._default_adv = None if default_adv is None else Decimal(str(default_adv))

    def _impact_fraction(self, quantity: Decimal, adv: Decimal | None) -> Decimal:
        if adv is None or adv <= 0 or quantity <= 0:
            return Decimal("0")
        # sqrt via Decimal for precision; ratio is dimensionless.
        ratio = quantity / adv
        frac = self._impact_coef * ratio.sqrt()
        return min(frac, self._max_fraction)

    def _apply_fraction(
        self, price: Price, side: OrderSide, fraction: Decimal
    ) -> Price:
        adjustment = price.value * fraction
        if side == OrderSide.SELL:
            adjustment = -adjustment
        return Price(value=price.value + adjustment)

    def apply_for_request(self, price: Price, request: Any) -> Price:
        """Instrument/timestamp-aware impact, preferred by the FillModel."""
        adv = self._default_adv
        if self._adv_provider is not None:
            adv = self._adv_provider(
                request.instrument, getattr(request, "reference_timestamp", None)
            )
        fraction = self._impact_fraction(request.quantity.value, adv)
        return self._apply_fraction(price, request.side, fraction)

    def apply(self, price: Price, side: OrderSide, quantity: Quantity) -> Price:
        """Protocol path: uses *default_adv* (no instrument/timestamp context)."""
        fraction = self._impact_fraction(quantity.value, self._default_adv)
        return self._apply_fraction(price, side, fraction)
