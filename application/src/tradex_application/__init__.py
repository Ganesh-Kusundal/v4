"""Application layer — use-case command handlers for TradeX v4.

Sits between HTTP routes (parse / HTTP-map) and ExecutionEngine (domain).
Each handler takes domain objects, dispatches to the engine, returns domain
results, and raises domain/application-level errors — zero HTTP coupling.
"""

from __future__ import annotations

from tradex_application.orders import (
    BrokerCapabilityError,
    build_bracket_request,
    cancel_order,
    modify_order,
    submit_bracket_order,
    submit_order,
)

__all__ = [
    "BrokerCapabilityError",
    "build_bracket_request",
    "cancel_order",
    "modify_order",
    "submit_bracket_order",
    "submit_order",
]

