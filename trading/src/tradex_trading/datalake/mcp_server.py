"""MCP server stub for datalake queries."""

from __future__ import annotations

from typing import Any

from tradex_trading.datalake.catalog import DataCatalog


class MCPDataLakeServer:
    """MCP server for datalake queries."""

    def __init__(self, catalog: DataCatalog) -> None:
        """Initialize server.

        Args:
            catalog: DataCatalog instance
        """
        self._catalog = catalog

    def start(self) -> None:
        """Start the MCP server (stub)."""

    def stop(self) -> None:
        """Stop the MCP server (stub)."""

    # -- query handlers -------------------------------------------------------

    def query_bars(
        self,
        symbol: str,
        start: Any | None = None,
        end: Any | None = None,
    ) -> list[dict]:
        """Query bars for *symbol* with optional date range."""
        return self._catalog.query_bars(symbol, start=start, end=end)

    def list_instruments(self) -> list[str]:
        """Return list of available instrument symbols."""
        return self._catalog.list_tables()

    def get_schema(self) -> list[str]:
        """Return bar column names."""
        return self._catalog.get_schema()

    def has_data(self, symbol: str) -> bool:
        """Return True when the catalog has bars for *symbol*."""
        return self._catalog.has_bars(symbol)


__all__ = ["MCPDataLakeServer"]
