"""Shared helpers for SDK services (leaf module — no service imports)."""

from __future__ import annotations

from typing import Any

from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.protocols import BrokerAdapter
from tradex_domain.value_objects import OrderId


def _broker_capabilities(broker: BrokerAdapter) -> BrokerCapabilities:
    """Fail-closed: a broker without a capabilities table claims nothing."""
    try:
        caps = broker.capabilities
    except AttributeError:
        return BrokerCapabilities()
    return caps or BrokerCapabilities()


def _as_order_id(value: Any) -> OrderId:
    """Coerce a raw value into an ``OrderId``."""
    return value if isinstance(value, OrderId) else OrderId(value=str(value))


__all__ = ["_as_order_id", "_broker_capabilities"]
