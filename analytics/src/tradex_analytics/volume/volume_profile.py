"""Volume profile: POC, VAH, VAL, LVN, value area."""

from __future__ import annotations


def poc(price_volume: dict[float, float]) -> float | None:
    """Price level with the highest volume; None when empty."""
    if not price_volume:
        return None
    return max(price_volume, key=price_volume.__getitem__)


def value_area(
    price_volume: dict[float, float], pct: float = 0.70,
) -> tuple[float | None, float | None]:
    """Return (VAL, VAH) bounding the *pct* fraction of total volume around POC.

    Walks outward from POC until accumulated volume >= pct * total.
    """
    if not price_volume:
        return (None, None)
    p = poc(price_volume)
    if p is None:
        return (None, None)
    total = sum(price_volume.values())
    target = total * pct
    sorted_prices = sorted(price_volume.keys())
    poc_idx = sorted_prices.index(p)
    accumulated = price_volume[p]
    lo_idx = poc_idx
    hi_idx = poc_idx
    while accumulated < target:
        expand_lo = lo_idx > 0
        expand_hi = hi_idx < len(sorted_prices) - 1
        if not expand_lo and not expand_hi:
            break
        lo_vol = price_volume[sorted_prices[lo_idx - 1]] if expand_lo else -1
        hi_vol = price_volume[sorted_prices[hi_idx + 1]] if expand_hi else -1
        if lo_vol >= hi_vol and expand_lo:
            lo_idx -= 1
            accumulated += lo_vol
        elif expand_hi:
            hi_idx += 1
            accumulated += hi_vol
        elif expand_lo:
            lo_idx -= 1
            accumulated += lo_vol
    return (sorted_prices[lo_idx], sorted_prices[hi_idx])


def vah(price_volume: dict[float, float], pct: float = 0.70) -> float | None:
    """Value Area High — upper bound of the value area."""
    return value_area(price_volume, pct)[1]


def val(price_volume: dict[float, float], pct: float = 0.70) -> float | None:
    """Value Area Low — lower bound of the value area."""
    return value_area(price_volume, pct)[0]


def lvn(price_volume: dict[float, float]) -> list[float]:
    """Low-Volume Nodes — price levels where volume is lower than both neighbors.

    Returns sorted list of prices. Empty profile or flat profile returns [].
    """
    if len(price_volume) < 3:
        return []
    sorted_prices = sorted(price_volume.keys())
    nodes: list[float] = []
    for i in range(1, len(sorted_prices) - 1):
        vol = price_volume[sorted_prices[i]]
        prev_vol = price_volume[sorted_prices[i - 1]]
        next_vol = price_volume[sorted_prices[i + 1]]
        if vol < prev_vol and vol < next_vol:
            nodes.append(sorted_prices[i])
    return nodes


