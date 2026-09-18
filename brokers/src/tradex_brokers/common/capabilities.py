"""Provider capability tables — re-exported from adapter modules.

Each broker owns its capability truth table in its adapter directory
(``dhan/capabilities.py``, ``upstox/capabilities.py``, ``paper/capabilities.py``).
This module re-exports them for backward compatibility.
"""

from __future__ import annotations

from tradex_domain.capabilities import BrokerCapabilities


def __getattr__(name: str) -> object:
    """Backward-compat: re-export broker capabilities from their new homes."""
    if name == "dhan_capabilities":
        from tradex_brokers.dhan.capabilities import dhan_capabilities
        return dhan_capabilities
    if name == "upstox_capabilities":
        from tradex_brokers.upstox.capabilities import upstox_capabilities
        return upstox_capabilities
    if name == "paper_capabilities":
        from tradex_brokers.paper.capabilities import paper_capabilities
        return paper_capabilities
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "dhan_capabilities",
    "paper_capabilities",
    "upstox_capabilities",
]
