"""StreamService — live subscription surface; reactive bus + optional backend."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tradex_domain.capabilities import BrokerCapabilities, require_capability
from tradex_domain.errors import CapabilityNotSupportedError
from tradex_domain.events import OrderFilled
from tradex_domain.market import Depth, Quote

from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.reactive.thread_safe_bus import ThreadSafeReactiveBus
from tradex_trading.sdk.streaming import BackendStreamSubscription, StreamSubscription


class StreamService:
    """Live subscription surface (D-15); reactive bus + optional backend."""

    def __init__(
        self,
        bus: ReactiveBus | ThreadSafeReactiveBus,
        subscriptions: list[StreamSubscription],
        capabilities: BrokerCapabilities,
        backend: Any = None,
    ) -> None:
        self._bus = bus
        self._subs = subscriptions
        self._capabilities = capabilities
        self._backend = backend

    # -- v4 reactive streams ---------------------------------------------------

    def subscribe_quotes(self, handler: Callable[[Quote], None]) -> StreamSubscription:
        """Subscribe to quote stream via the reactive bus."""
        d = self._bus.of_type(Quote).subscribe(handler)
        sub = StreamSubscription(d, "quotes")
        self._subs.append(sub)
        return sub

    def subscribe_fills(self, handler: Callable[[OrderFilled], None]) -> StreamSubscription:
        """Subscribe to fill stream via the reactive bus."""
        d = self._bus.of_type(OrderFilled).subscribe(handler)
        sub = StreamSubscription(d, "fills")
        self._subs.append(sub)
        return sub

    def subscribe_cancellations(
        self, handler: Callable[[OrderCancelled], None]
    ) -> StreamSubscription:
        """Subscribe to order-cancellation events via the reactive bus."""
        from tradex_domain.events import OrderCancelled

        d = self._bus.of_type(OrderCancelled).subscribe(handler)
        sub = StreamSubscription(d, "cancellations")
        self._subs.append(sub)
        return sub

    def subscribe_modifications(
        self, handler: Callable[[OrderModified], None]
    ) -> StreamSubscription:
        """Subscribe to order-modification events via the reactive bus."""
        from tradex_domain.events import OrderModified

        d = self._bus.of_type(OrderModified).subscribe(handler)
        sub = StreamSubscription(d, "modifications")
        self._subs.append(sub)
        return sub

    def subscribe_depth(self, handler: Callable[[Depth], None]) -> StreamSubscription:
        """Subscribe to depth stream via the reactive bus."""
        d = self._bus.of_type(Depth).subscribe(handler)
        sub = StreamSubscription(d, "depth")
        self._subs.append(sub)
        return sub

    # -- v3-parity streams -----------------------------------------------------

    def subscribe_orders(self, handler: Callable[[object], None]) -> StreamSubscription:
        """Subscribe to order-update stream (v3 parity)."""
        sub: StreamSubscription
        if self._backend is not None:
            sub = BackendStreamSubscription(
                self._backend, self._backend.subscribe_orders(handler), "orders"
            )
            self._subs.append(sub)
            return sub
        # Fallback: reactive bus OrderPlaced stream
        from tradex_domain.events import OrderPlaced

        d = self._bus.of_type(OrderPlaced).subscribe(handler)
        sub = StreamSubscription(d, "orders")
        self._subs.append(sub)
        return sub

    def subscribe_positions(self, handler: Callable[[object], None]) -> StreamSubscription:
        """Subscribe to position-update stream (v3 parity)."""
        require_capability(self._capabilities, "supports_portfolio_stream")
        if self._backend is not None:
            sub = BackendStreamSubscription(
                self._backend, self._backend.subscribe_positions(handler), "positions"
            )
            self._subs.append(sub)
            return sub
        raise CapabilityNotSupportedError("no stream backend bound for position stream")

    def unsubscribe(self, subscription: StreamSubscription) -> None:
        """Cancel a single subscription."""
        subscription.cancel()
        try:
            self._subs.remove(subscription)
        except ValueError:
            pass

    def close(self) -> None:
        """Close the stream backend (if bound)."""
        if self._backend is not None:
            self._backend.close()


__all__ = ["StreamService"]
