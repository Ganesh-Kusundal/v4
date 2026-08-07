"""AnalyticsService — indicator/report evaluation over a bound engine."""

from __future__ import annotations

from typing import Any

from tradex_domain.errors import CapabilityNotSupportedError
from tradex_domain.market import HistoricalSeries


class AnalyticsService:
    """Indicator/report evaluation over a bound engine (D-15)."""

    def __init__(self, engine: Any = None) -> None:
        self._engine = engine

    def indicators(self, series: Any, names: list[str], **params: Any) -> Any:
        """Calculate indicators on series. Stub until Phase 10."""
        if self._engine is not None:
            return self._engine.indicators(series, names, **params)
        raise CapabilityNotSupportedError("analytics engine is not bound to this session")

    def indicator(
        self,
        series: HistoricalSeries,
        name: str,
        **params: Any,
    ) -> HistoricalSeries:
        """Calculate a single indicator (v3 parity)."""
        if self._engine is None:
            raise CapabilityNotSupportedError("analytics engine is not bound to this session")
        return self._engine.indicator(series, name, **params)

    def report(
        self,
        name: str,
        series: HistoricalSeries,
        **params: Any,
    ) -> dict[str, Any]:
        """Generate an analytics report (v3 parity)."""
        if self._engine is None:
            raise CapabilityNotSupportedError("analytics engine is not bound to this session")
        return self._engine.report(name, series, **params)


__all__ = ["AnalyticsService"]
