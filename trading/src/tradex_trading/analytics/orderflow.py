"""Order-flow imbalance, aggressor classification, and CVD."""

from __future__ import annotations


def imbalance(bid_size: float, ask_size: float) -> float:
    """Return bid/ask imbalance ratio in [-1, 1]. Zero when both are zero."""
    total = bid_size + ask_size
    if total == 0:
        return 0.0
    return (bid_size - ask_size) / total


def classify_aggressor(*, ltp: float, bid: float, ask: float) -> int:
    """Classify a trade's aggressor side.

    Returns +1 (buyer aggressive), -1 (seller aggressive), or 0 (mid).
    ponytail: simple midpoint classification. Real tick data has exchange
    tick rule; this approximation works for Quote-level data.
    """
    mid = (bid + ask) / 2.0
    if ltp > mid:
        return 1
    if ltp < mid:
        return -1
    return 0


def cvd_from_quotes(quotes: list) -> list[int]:
    """Cumulative Volume Delta from a list of Quote events.

    Returns a list of cumulative delta values (one per quote).
    Each quote's volume is added if buyer-aggressive, subtracted if seller.
    """
    cumulative = 0
    result: list[int] = []
    for q in quotes:
        if q.volume is None:
            result.append(cumulative)  # no volume — no delta contribution
            continue
        ltp = float(q.ltp.value)
        bid = float(q.bid.value) if q.bid is not None else ltp
        ask = float(q.ask.value) if q.ask is not None else ltp
        direction = classify_aggressor(ltp=ltp, bid=bid, ask=ask)
        vol = int(q.volume.value)
        cumulative += direction * vol
        result.append(cumulative)
    return result


__all__ = ["imbalance", "classify_aggressor", "cvd_from_quotes"]
