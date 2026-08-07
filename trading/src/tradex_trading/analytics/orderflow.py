"""Order-flow imbalance from bid/ask sizes."""

from __future__ import annotations


def imbalance(bid_size: float, ask_size: float) -> float:
    """Return bid/ask imbalance ratio in [-1, 1]. Zero when both are zero."""
    total = bid_size + ask_size
    if total == 0:
        return 0.0
    return (bid_size - ask_size) / total


__all__ = ["imbalance"]
