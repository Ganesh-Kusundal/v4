"""FillModel — the single fill-resolution path shared by every execution mode.

CROSS-MODE PARITY: backtest (SimulatedFillSource), paper (PaperFillSource) and
live (BrokerFillSource) all build their Fill from the same price-resolution +
slippage + timestamp rules, so the same input events produce identical fill
prices and quantities in every mode (HIGH-6b).
"""

from __future__ import annotations

from datetime import UTC, datetime

from tradex_domain.enums import OrderSide, OrderType
from tradex_domain.execution import Fill, Order, OrderRequest
from tradex_domain.value_objects import Price


class FillModel:
    """Encapsulates the COMMON fill logic shared by all fill sources.

    Each fill source keeps only its mode-specific concerns — Paper's LTP
    cache lookup, Broker's ACK-only submission — and delegates price
    resolution, slippage, the ValueError-on-zero-price guard, and
    deterministic Fill construction here.
    """

    def __init__(
        self,
        slippage_model: object | None = None,
        *,
        clock: object | None = None,
        require_reference_timestamp: bool = False,
    ) -> None:
        self._slippage_model = slippage_model
        self._clock = clock
        self._require_reference_timestamp = require_reference_timestamp

    def resolve_fill_price(
        self,
        request: OrderRequest,
        market_price: Price | None = None,
    ) -> Price:
        """Resolve the fill price for *request* and apply slippage.

        Uses *market_price* (e.g. an LTP quote / next bar's open reference)
        when provided; otherwise falls back to the request's limit price, then
        its trigger price. Raises ValueError on a non-positive price so a
        zero-priced fill can never silently corrupt P&L (avg_price=0).

        Slippage (if configured) is applied identically for every source, so
        net P&L agrees across backtest/paper/live for the same events.
        """
        price = market_price
        if price is None:
            price = request.price or request.trigger_price
        if price is None or price.value <= 0:
            raise ValueError(
                f"cannot fill {request.instrument} ({request.side.value}) "
                f"without a positive price"
            )
        if self._slippage_model is not None and hasattr(
            self._slippage_model, "apply"
        ):
            price = self._slippage_model.apply(
                price, request.side, request.quantity
            )
        if request.order_type == OrderType.LIMIT and request.price is not None:
            if request.side == OrderSide.BUY and price.value > request.price.value:
                price = request.price  # ponytail: limit is hard, slippage cannot worsen beyond it
            elif request.side == OrderSide.SELL and price.value < request.price.value:
                price = request.price  # ponytail: limit is hard, slippage cannot worsen beyond it
        return price

    def fill_timestamp(self, request: OrderRequest) -> datetime:
        """Resolve the fill timestamp.

        Deterministic simulation may require the market-data reference time;
        live receipt paths intentionally retain a clock fallback.
        """
        if request.reference_timestamp is not None:
            return request.reference_timestamp
        if self._require_reference_timestamp:
            raise ValueError(
                "deterministic fill requires OrderRequest.reference_timestamp"
            )
        if self._clock is not None and hasattr(self._clock, "now"):
            return self._clock.now()
        return datetime.now(UTC)

    def make_fill(
        self,
        order: Order,
        price: Price,
        timestamp: datetime,
    ) -> Fill:
        """Build a Fill from an order + resolved price."""
        return Fill(
            order_id=order.order_id,
            instrument=order.instrument,
            side=order.side,
            quantity=order.quantity,
            price=price,
            timestamp=timestamp,
            # A fill is priced from its own product's schedule, and a null is
            # resolved to INTRADAY and counted, so the label has to be carried
            # here — the one place every fill source builds its Fill.
            product_type=order.product_type,
            # Same reason: attribution is only knowable from the order, so it
            # is stamped here rather than inferred downstream.
            strategy_id=order.strategy_id,
        )


