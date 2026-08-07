"""Probability helpers."""

from __future__ import annotations


def win_rate(pnls: list[float]) -> float:
    """Fraction of profitable trades. Zero when empty."""
    if not pnls:
        return 0.0
    return sum(1 for p in pnls if p > 0) / len(pnls)


__all__ = ["win_rate"]
