"""Market breadth: advance / decline counts, ratio, breadth indicator, and TRIN."""

from __future__ import annotations


def advance_decline(returns: list[float]) -> tuple[int, int, float]:
    """Return (advances, declines, adv/dec ratio). Flat (0) returns are ignored."""
    advances = sum(1 for r in returns if r > 0)
    declines = sum(1 for r in returns if r < 0)
    ratio = (
        advances / declines
        if declines
        else float("inf") if advances else 0.0
    )
    return advances, declines, ratio


def breadth_indicator(returns_by_symbol: dict[str, list[float]]) -> list[dict]:
    """Compute per-period breadth across multiple symbols.

    For each time-period index, counts how many symbols advanced vs declined,
    computes the advance/decline ratio, and tracks the cumulative product of
    ratios.

    Returns a list of dicts with keys: ``period``, ``advances``, ``declines``,
    ``ratio``, ``cumulative``.
    """
    if not returns_by_symbol:
        return []

    max_len = max(len(v) for v in returns_by_symbol.values())
    symbols = list(returns_by_symbol.keys())
    cumulative = 1.0
    result: list[dict] = []

    for period in range(max_len):
        advances = 0
        declines = 0
        for sym in symbols:
            vals = returns_by_symbol[sym]
            if period < len(vals):
                r = vals[period]
                if r > 0:
                    advances += 1
                elif r < 0:
                    declines += 1
        ratio = advances / declines if declines else float("inf") if advances else 0.0
        if ratio != 0.0 and ratio != float("inf"):
            cumulative *= ratio
        elif ratio == float("inf"):
            cumulative = float("inf")
        result.append(
            {
                "period": period,
                "advances": advances,
                "declines": declines,
                "ratio": ratio,
                "cumulative": cumulative,
            }
        )
    return result


def trin(
    advances: int,
    declines: int,
    advance_volume: float,
    decline_volume: float,
) -> float:
    """Compute the TRIN (Arms Index).

    TRIN = (declines / advances) / (decline_volume / advance_volume)

    Guards against zero division — returns ``float('inf')`` when the
    denominator is zero.
    """
    if advances == 0 or advance_volume == 0:
        return float("inf")
    ad_ratio = declines / advances
    vol_ratio = decline_volume / advance_volume
    if vol_ratio == 0:
        return float("inf")
    return ad_ratio / vol_ratio


