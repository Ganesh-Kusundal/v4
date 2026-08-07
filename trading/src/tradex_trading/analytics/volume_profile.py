"""Volume profile: point of control (POC)."""

from __future__ import annotations


def poc(price_volume: dict[float, float]) -> float | None:
    """Price level with the highest volume; None when empty."""
    if not price_volume:
        return None
    return max(price_volume, key=price_volume.__getitem__)


__all__ = ["poc"]
