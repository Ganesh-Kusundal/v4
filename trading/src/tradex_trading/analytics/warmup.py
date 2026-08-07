"""Indicator warm-up support.

Provides wrappers that suppress indicator output during the initial
warm-up period, preventing false signals from indicators that need
historical data to stabilize.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Indicator(Protocol):
    """Protocol for indicators that can be updated with new values."""

    def update(self, value: Any) -> Any: ...


class WarmupFilter:
    """Indicator wrapper that suppresses output during the warm-up period.

    The first `period` updates return None, allowing the underlying indicator
    to accumulate sufficient data before producing valid signals.
    """

    def __init__(self, indicator: Indicator, period: int) -> None:
        self._indicator = indicator
        self._period = period
        self._count = 0

    def update(self, value: Any) -> Any:
        self._count += 1
        result = self._indicator.update(value)
        if self._count <= self._period:
            return None  # Warm-up period — suppress output
        return result

    @property
    def warmed_up(self) -> bool:
        """Whether the warm-up period has elapsed."""
        return self._count > self._period

    @property
    def count(self) -> int:
        """Number of updates received so far."""
        return self._count


def warmup_indicator(indicator: Indicator, period: int) -> WarmupFilter:
    """Helper to wrap an indicator with warm-up support.

    Args:
        indicator: The indicator to wrap.
        period: Number of initial bars to suppress.

    Returns:
        A WarmupFilter wrapping the indicator.
    """
    return WarmupFilter(indicator, period)


__all__ = [
    "Indicator",
    "WarmupFilter",
    "warmup_indicator",
]
