"""Parquet+DuckDB backed DataCatalog (spec §07)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class ParquetDataCatalog:
    """Columnar data catalog using Parquet files + DuckDB queries.

    Each instrument+timeframe gets a .parquet file. DuckDB enables
    SQL queries directly on Parquet files without loading into memory.

    Requires: pyarrow, duckdb (optional dependencies).
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _parquet_path(self, symbol: str, timeframe: str) -> Path:
        safe_symbol = symbol.replace(":", "_").replace("/", "_")
        return self._root / f"{safe_symbol}_{timeframe}.parquet"

    def has_bars(self, symbol: str, timeframe: str) -> bool:
        return self._parquet_path(symbol, timeframe).exists()

    def write_bars(self, symbol: str, timeframe: str, bars: list[dict[str, Any]]) -> None:
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            raise ImportError("pyarrow is required for ParquetDataCatalog")

        if not bars:
            return

        path = self._parquet_path(symbol, timeframe)
        table = pa.table({
            "timestamp": [b["timestamp"] for b in bars],
            "open": [float(b["open"]) for b in bars],
            "high": [float(b["high"]) for b in bars],
            "low": [float(b["low"]) for b in bars],
            "close": [float(b["close"]) for b in bars],
            "volume": [float(b.get("volume", 0)) for b in bars],
        })
        pq.write_table(table, path)
        log.info("Wrote %d bars to %s", len(bars), path)

    def read_bars(self, symbol: str, timeframe: str) -> list[dict[str, Any]]:
        try:
            import pyarrow.parquet as pq
        except ImportError:
            raise ImportError("pyarrow is required for ParquetDataCatalog")

        path = self._parquet_path(symbol, timeframe)
        if not path.exists():
            return []

        table = pq.read_table(path)
        return table.to_pydict()

    def query(self, sql: str) -> Any:
        """Execute a DuckDB SQL query against the parquet files."""
        try:
            import duckdb
        except ImportError:
            raise ImportError("duckdb is required for SQL queries")

        conn = duckdb.connect()
        conn.execute(f"SET file_search_path='{self._root}'")
        return conn.execute(sql).fetchall()

    def list_tables(self) -> list[str]:
        return [p.stem for p in self._root.glob("*.parquet")]

    def get_schema(self, symbol: str, timeframe: str) -> dict[str, str] | None:
        try:
            import pyarrow.parquet as pq
        except ImportError:
            raise ImportError("pyarrow is required for ParquetDataCatalog")

        path = self._parquet_path(symbol, timeframe)
        if not path.exists():
            return None
        schema = pq.read_schema(path)
        return {field.name: str(field.type) for field in schema}


__all__ = ["ParquetDataCatalog"]
