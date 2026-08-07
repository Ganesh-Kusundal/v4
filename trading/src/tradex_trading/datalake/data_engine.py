"""DataEngine — unified data facade orchestrating catalog, quality, and source selection."""

from __future__ import annotations

from typing import Any

from tradex_trading.datalake.catalog import DataCatalog
from tradex_trading.datalake.quality import DataQualityEngine
from tradex_trading.datalake.source_selection import SourceSelectionPolicy


class DataEngine:
    """Unified facade for data access, quality validation, and source selection."""

    def __init__(
        self,
        catalog: DataCatalog,
        quality_engine: DataQualityEngine | None = None,
        source_policy: SourceSelectionPolicy | None = None,
    ) -> None:
        self._catalog = catalog
        self._quality_engine = quality_engine
        self._source_policy = source_policy

    def get_bars(
        self,
        instrument: Any,
        timeframe: Any,
        start: Any | None = None,
        end: Any | None = None,
    ) -> list:
        """Read bars from catalog, optionally using source_policy to select source."""
        if self._source_policy is not None:
            self._source_policy.select(instrument, timeframe, start, end)
        return self._catalog.read_bars(instrument, timeframe, start=start, end=end)

    def validate_bars(self, bars: list, timeframe: Any | None = None) -> list[str]:
        """Run quality checks if quality_engine is available, else return empty list."""
        if self._quality_engine is None:
            return []
        issues: list[str] = []
        if timeframe is not None:
            issues.extend(self._quality_engine.check_gaps(bars, timeframe))
        issues.extend(self._quality_engine.check_duplicates(bars))
        issues.extend(self._quality_engine.check_ohlc_integrity(bars))
        issues.extend(self._quality_engine.check_volume(bars))
        return issues

    def list_available_data(self) -> list:
        """Return list of instruments available in catalog."""
        return self._catalog.list_instruments()

    def has_data(self, instrument: str) -> bool:
        """Check if catalog has bars for *instrument*."""
        return self._catalog.has_bars(instrument)

    def get_schema(self) -> list[str]:
        """Return bar column names from catalog."""
        return self._catalog.get_schema()


__all__ = ["DataEngine"]
