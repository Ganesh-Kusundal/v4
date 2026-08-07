"""Fundamental ratios."""

from __future__ import annotations


def pe_ratio(price: float, eps: float) -> float:
    """Price-to-earnings ratio. Raises ValueError when eps is zero."""
    if eps == 0:
        raise ValueError("eps must be non-zero")
    return price / eps


__all__ = ["pe_ratio"]
