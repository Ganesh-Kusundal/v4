"""Sector strength ranking."""

from __future__ import annotations


def sector_strength(
    sector_returns: dict[str, float],
) -> list[tuple[str, float]]:
    """Return (sector, return) pairs sorted by return descending."""
    return sorted(
        sector_returns.items(), key=lambda item: item[1], reverse=True,
    )


__all__ = ["sector_strength"]
