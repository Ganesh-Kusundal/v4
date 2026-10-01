"""Single interval normalization path.

``D`` is the chart REST canon for a daily bar; the domain Timeframe and the
WebSocket spell it ``1d``; ``1D`` and ``d`` are the common hand-typed
variants. One function answers for every surface so the alias family cannot
drift between them.
"""

#: Every accepted spelling of a daily bar.
_DAILY_ALIASES = frozenset({"1d", "1D", "d", "D"})


def normalize_interval(raw: str) -> str:
    """Canonical chart-style interval: every daily spelling becomes ``D``."""
    if raw in _DAILY_ALIASES:
        return "D"
    return raw
