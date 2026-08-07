"""Source selection policy — determines data source for instruments."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class DataSourceKind(StrEnum):
    """Enumeration of possible data-source backends."""

    DATALAKE = "DATALAKE"
    BROKER_HISTORICAL = "BROKER_HISTORICAL"
    LIVE = "LIVE"
    REPLAY = "REPLAY"


@dataclass(frozen=True, slots=True)
class SourceSelectionPolicy:
    """Policy for selecting data sources.

    Priority: datalake (if available and fresh) → broker historical → live
    """

    preferred_source: str = "broker"
    fallback_source: str = "datalake"
    _catalog: Any = field(default=None, compare=False)

    def select(self, instrument, timeframe, start=None, end=None) -> str:
        """Select the best data source for the given request.

        Priority: datalake (if available and fresh) → broker historical → live

        Args:
            instrument: Instrument instance
            timeframe: Timeframe enum
            start: Optional start datetime for historical range
            end: Optional end datetime for historical range

        Returns:
            Source name ('broker' or 'datalake')
        """
        # If we have a catalog reference, check if datalake has the data
        if self._catalog is not None:
            try:
                if self._catalog.has_bars(instrument, timeframe):
                    return DataSourceKind.DATALAKE
            except Exception:
                pass

        # For historical ranges, prefer broker
        if start is not None or end is not None:
            return DataSourceKind.BROKER_HISTORICAL

        # Default to live
        return self.preferred_source


__all__ = ["DataSourceKind", "SourceSelectionPolicy"]
