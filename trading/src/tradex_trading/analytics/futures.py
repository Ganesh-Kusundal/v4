"""Futures basis = futures price - spot price."""

from __future__ import annotations


def basis(futures: float, spot: float) -> float:
    """Return the basis (futures - spot)."""
    return futures - spot


__all__ = ["basis"]
