"""ExtensionService — capability-exposed broker features (D-16)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from tradex_domain.capabilities import BrokerCapabilities, require_capability
from tradex_domain.enums import OrderStatus
from tradex_domain.execution import OrderReceipt, OrderRequest, OrderResult
from tradex_domain.value_objects import OrderId

from tradex_trading.sdk.services._helpers import _as_order_id

# ---------------------------------------------------------------------------
# Extension result types (D-16)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KillSwitchResult:
    """Typed result from a kill-switch action or status query."""

    status: str
    message: str = ""


@dataclass(frozen=True)
class EdisStatus:
    """Typed result from an eDIS status query."""

    status: str
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TpinResult:
    """Typed result from a generate_tpin request."""

    status: str
    message: str = ""


class ExtensionService:
    """Capability-exposed broker features (D-16): super/forever/slice order, eDIS."""

    def __init__(
        self,
        broker: Any,
        capabilities: BrokerCapabilities | None = None,
        order_gate: Callable[[], None] | None = None,
    ) -> None:
        self._broker = broker
        self._capabilities = capabilities or BrokerCapabilities()
        self._order_gate = order_gate

    def is_extension_adapter(self) -> bool:
        """Check if broker supports extension adapter protocol."""
        from tradex_domain.protocols import ExtensionAdapter

        return isinstance(self._broker, ExtensionAdapter)

    def _require_order_gate(self) -> None:
        if self._order_gate is not None:
            self._order_gate()

    @staticmethod
    def _receipt(order_id: OrderId) -> OrderReceipt:
        return OrderReceipt(order_id=_as_order_id(order_id), status=OrderStatus.SUBMITTED)

    # -- super orders ----------------------------------------------------------

    def super_order(self, request: OrderRequest) -> OrderResult:
        self._require_order_gate()
        require_capability(self._capabilities, "supports_super_order")
        order_id = self._broker.submit_super_order(request)
        return OrderResult(order_id=order_id, status=OrderStatus.SUBMITTED)

    def modify_super(self, order_id: OrderId, request: OrderRequest) -> OrderResult:
        self._require_order_gate()
        require_capability(self._capabilities, "supports_super_order")
        return self._broker.modify_super_order(_as_order_id(order_id), request)

    def cancel_super(self, order_id: OrderId, leg: str = "ENTRY") -> OrderResult:
        self._require_order_gate()
        require_capability(self._capabilities, "supports_super_order")
        return self._broker.cancel_super_order(_as_order_id(order_id), leg)

    def list_super(self) -> list[OrderResult]:
        require_capability(self._capabilities, "supports_super_order")
        return self._broker.list_super_orders()

    # -- forever orders --------------------------------------------------------

    def forever_order(self, request: OrderRequest) -> OrderResult:
        self._require_order_gate()
        require_capability(self._capabilities, "supports_forever_order")
        order_id = self._broker.submit_forever_order(request)
        return OrderResult(order_id=order_id, status=OrderStatus.SUBMITTED)

    def modify_forever(self, order_id: OrderId, request: OrderRequest) -> OrderResult:
        self._require_order_gate()
        require_capability(self._capabilities, "supports_forever_order")
        return self._broker.modify_forever_order(_as_order_id(order_id), request)

    def cancel_forever(self, order_id: OrderId) -> OrderResult:
        self._require_order_gate()
        require_capability(self._capabilities, "supports_forever_order")
        return self._broker.cancel_forever_order(_as_order_id(order_id))

    def get_forever(self) -> list[OrderResult]:
        require_capability(self._capabilities, "supports_forever_order")
        return self._broker.list_forever_orders()

    # -- slice orders ----------------------------------------------------------

    def slice_order(
        self,
        request: OrderRequest,
        slices: int,
        interval: timedelta | None = None,
    ) -> list[OrderResult]:
        self._require_order_gate()
        require_capability(self._capabilities, "supports_slice_order")
        order_ids = self._broker.submit_slice_order(request, slices, interval)
        return [
            OrderResult(order_id=oid, status=OrderStatus.SUBMITTED)
            for oid in order_ids
        ]

    # -- eDIS ------------------------------------------------------------------

    def edis(self, request: OrderRequest) -> EdisStatus:
        self._require_order_gate()
        require_capability(self._capabilities, "supports_edis")
        raw = self._broker.submit_edis(request)
        return EdisStatus(status="SUBMITTED", details={"order_id": str(raw)})

    def generate_tpin(self) -> TpinResult:
        require_capability(self._capabilities, "supports_edis")
        raw = self._broker.generate_tpin()
        return TpinResult(
            status=str(raw.get("status", "")),
            message=str(raw.get("message", "")),
        )

    def edis_status(self, isin: str) -> EdisStatus:
        require_capability(self._capabilities, "supports_edis")
        raw = self._broker.edis_status(isin)
        return EdisStatus(
            status=str(raw.get("status", "")),
            details={k: v for k, v in raw.items() if k != "status"},
        )

    # -- kill switch -----------------------------------------------------------

    def kill_switch(self, enable: bool = True) -> KillSwitchResult:
        """Broker-side kill switch (D-16); mutating, so order-gated."""
        self._require_order_gate()
        require_capability(self._capabilities, "supports_kill_switch")
        raw = self._broker.kill_switch(enable)
        return KillSwitchResult(
            status=str(raw.get("status", "")),
            message=str(raw.get("message", "")),
        )

    def status_kill_switch(self) -> KillSwitchResult:
        require_capability(self._capabilities, "supports_kill_switch")
        raw = self._broker.status_kill_switch()
        return KillSwitchResult(
            status=str(raw.get("status", "")),
            message=str(raw.get("message", "")),
        )

    # -- discovery API ---------------------------------------------------------

    def list_available(self) -> list[str]:
        """List all extension methods available on the current broker."""
        methods: list[str] = []
        for name in dir(self):
            if name.startswith("_"):
                continue
            if name in ("list_available", "capabilities"):
                continue
            attr = getattr(self, name)
            if callable(attr):
                methods.append(name)
        return sorted(methods)

    def capabilities(self) -> dict[str, bool]:
        """Return the capability map for the current broker's extension features."""
        return {
            "supports_super_order": self._capabilities.supports_super_order,
            "supports_forever_order": self._capabilities.supports_forever_order,
            "supports_slice_order": self._capabilities.supports_slice_order,
            "supports_edis": self._capabilities.supports_edis,
            "supports_kill_switch": self._capabilities.supports_kill_switch,
        }


__all__ = [
    "EdisStatus",
    "ExtensionService",
    "KillSwitchResult",
    "OrderResult",
    "TpinResult",
]
