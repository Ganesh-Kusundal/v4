"""Reactive strategy engine — strategies subscribe to typed Observable streams."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tradex_domain.enums import OrderType
from tradex_domain.events import PlaceOrderCommand
from tradex_domain.execution import OrderRequest
from tradex_domain.strategy import Signal, StrategyContext
from tradex_domain.value_objects import Quantity


class ReactiveStrategyEngine:
    """Strategies subscribe to typed Observable streams via the bus."""

    def __init__(self, bus) -> None:
        """Initialize with a ReactiveBus.

        Args:
            bus: ReactiveBus instance for event streaming
        """
        self._bus = bus
        self._strategies: dict[str, Any] = {}
        self._disposables: list = []
        self._bar_count = 0

    def _make_context(self, **overrides: Any) -> StrategyContext:
        """Create a StrategyContext with current engine state."""
        return StrategyContext(**overrides)

    def _wrap_on_bar(self, strategy: Any) -> Any:
        """Wrap strategy.on_bar to inject context and bridge Signal→Order."""
        def handler(candle: Any) -> Any:
            self._bar_count += 1
            ctx = self._make_context(
                bar_count=self._bar_count,
                timestamp=getattr(candle, "timestamp", None),
            )
            result = strategy.on_bar(ctx, candle)
            self._maybe_publish_order(result, strategy.strategy_id)
            return result
        return handler

    def _wrap_on_quote(self, strategy: Any) -> Any:
        """Wrap strategy.on_quote to inject context and bridge Signal→Order."""
        def handler(quote: Any) -> Any:
            ctx = self._make_context(timestamp=getattr(quote, "timestamp", None))
            result = strategy.on_quote(ctx, quote)
            self._maybe_publish_order(result, strategy.strategy_id)
            return result
        return handler

    def _wrap_on_fill(self, strategy: Any) -> Any:
        """Wrap strategy.on_fill to inject context."""
        def handler(fill: Any) -> Any:
            ctx = self._make_context()
            return strategy.on_fill(ctx, fill)
        return handler

    def _maybe_publish_order(self, result: Any, strategy_id: str) -> None:
        """If *result* is a Signal, publish a PlaceOrderCommand to the bus."""
        if not isinstance(result, Signal):
            return
        qty_value = abs(result.strength) if result.strength else 1.0
        request = OrderRequest(
            instrument=result.instrument,
            side=result.direction,
            order_type=OrderType.MARKET,
            quantity=Quantity(Decimal(str(qty_value))),
            tag=strategy_id,
        )
        self._bus.publish(PlaceOrderCommand(request=request))

    def register(self, strategy) -> None:
        """Register a strategy and subscribe it to market events.

        Args:
            strategy: Strategy instance implementing the Strategy protocol
        """
        self._strategies[strategy.strategy_id] = strategy

        from tradex_domain import Candle, Fill, Quote

        self._disposables.extend([
            self._bus.of_type(Candle).subscribe(self._wrap_on_bar(strategy)),
            self._bus.of_type(Quote).subscribe(self._wrap_on_quote(strategy)),
            self._bus.of_type(Fill).subscribe(self._wrap_on_fill(strategy)),
        ])

    def unregister(self, strategy_id: str) -> None:
        """Unregister a strategy by ID.

        Args:
            strategy_id: Unique identifier of the strategy to remove
        """
        self._strategies.pop(strategy_id, None)

    def dispose_all(self) -> None:
        """Clean up all subscriptions."""
        for disposable in self._disposables:
            try:
                disposable.dispose()
            except Exception:  # pragma: no cover
                pass
        self._disposables.clear()
        self._strategies.clear()

    @property
    def strategies(self) -> dict:
        """Return registered strategies."""
        return dict(self._strategies)


__all__ = ["ReactiveStrategyEngine"]
