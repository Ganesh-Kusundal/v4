"""TradeService — order placement, cancel/modify."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tradex_domain.capabilities import BrokerCapabilities, require_capability
from tradex_domain.enums import OrderType
from tradex_domain.errors import CapabilityNotSupportedError
from tradex_domain.execution import Order, OrderReceipt, OrderRequest
from tradex_domain.protocols import BrokerAdapter
from tradex_domain.value_objects import OrderId

from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.reactive.thread_safe_bus import ThreadSafeReactiveBus
from tradex_trading.sdk.services._helpers import _as_order_id


class TradeService:
    """Order placement, cancel/modify (D-8, D-14)."""

    _TYPE_CAPABILITY = {
        OrderType.MARKET: "supports_market_order",
        OrderType.LIMIT: "supports_limit_order",
        OrderType.STOP: "supports_stop_order",
        OrderType.STOP_LIMIT: "supports_stop_order",
    }

    def __init__(
        self,
        engine: ExecutionEngine,
        bus: ReactiveBus | ThreadSafeReactiveBus,
        broker: BrokerAdapter | None = None,
        capabilities: BrokerCapabilities | None = None,
        order_gate: Callable[[], None] | None = None,
    ) -> None:
        self._engine = engine
        self._bus = bus
        self._broker = broker
        self._capabilities = capabilities or BrokerCapabilities()
        self._order_gate = order_gate

    # -- v4 canonical path -----------------------------------------------------

    def submit(self, request: OrderRequest) -> OrderReceipt:
        """Submit an order through the execution engine (v4 canonical path)."""
        self._require_order_gate()
        cap = self._TYPE_CAPABILITY.get(request.order_type)
        if cap is not None:
            require_capability(self._capabilities, cap)
        return self._engine.submit(request)

    def cancel(self, order_id: Any) -> Order:
        """Cancel an order through the execution engine."""
        self._require_order_gate()
        return self._engine.cancel(_as_order_id(order_id))

    # -- broker-delegated methods (capability-loud) ---------------------------

    def _require_order_gate(self) -> None:
        if self._order_gate is not None:
            self._order_gate()

    def bind_execution_engine(self, execution_engine: ExecutionEngine) -> None:
        """Bind the canonical mutation spine after runtime composition."""
        self._engine = execution_engine

    def modify_order(self, order_id: OrderId, request: OrderRequest) -> Order:
        """Modify via broker with capability check (v3 parity)."""
        self._require_order_gate()
        require_capability(self._capabilities, "supports_modify")
        if self._broker is not None:
            return self._broker.modify_order(order_id, request)
        raise CapabilityNotSupportedError("no broker bound for modify_order")

    def get_order(self, order_id: OrderId | str) -> Order:
        """Fetch a single order — engine OMS cache first, broker fallback.

        The v4 canonical path routes submissions through the execution
        engine, whose cache is the session's order state (boot()/paper()/
        live() all share it with the session). The broker is consulted only
        when the engine has no record (e.g. orders placed outside the
        session). Accepts a raw string id (as callers like the HTTP API
        hand in) via the usual ``_as_order_id`` coercion.
        """
        oid = _as_order_id(order_id)
        order = self._engine.get_order(oid)
        if order is not None:
            return order
        if self._broker is not None:
            return self._broker.get_order(oid)
        raise CapabilityNotSupportedError("no broker bound for get_order")

    def get_orderbook(self) -> list[Order]:
        """Fetch the full order book — engine OMS cache first, broker fallback.

        The engine cache is the session's order state (the composition root
        shares it with the session), so it is authoritative once it holds
        anything. The broker is consulted only when the OMS cache is empty —
        for live brokers that means orders placed entirely outside this
        session (other clients, the broker app) are visible only until the
        session places its first order. Paper/backtest sessions are fully
        covered by the engine cache.
        """
        orders = list(self._engine.all_orders())
        if orders:
            return orders
        if self._broker is not None:
            return list(self._broker.get_orderbook())
        return []


__all__ = ["TradeService"]
