"""Universe ranking helpers."""

from __future__ import annotations


def rank_by_return(returns: dict[str, float]) -> list[str]:
    """Return symbols ranked by return (descending)."""
    return sorted(returns, key=returns.__getitem__, reverse=True)


__all__ = ["rank_by_return"]
