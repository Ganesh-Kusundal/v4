"""Bar source classification — where a bar's ticks came from.

Metadata, not a routing key: computational consumers (indicators,
strategies) must not filter on this. Only display and integrity
consumers check it (replay tracking, forming-bar throttle).
"""

from __future__ import annotations

from enum import Enum


class BarSource(Enum):
    """The origin of a bar's ticks."""

    LIVE = "live"
    SIM = "sim"
    BACKTEST = "backtest"

    @classmethod
    def coerce(cls, value: BarSource | str) -> BarSource:
        """Coerce a raw string to a BarSource, defaulting to LIVE."""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return cls.LIVE


__all__ = ["BarSource"]
