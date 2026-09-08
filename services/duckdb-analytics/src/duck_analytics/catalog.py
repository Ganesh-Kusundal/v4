"""DuckDBCatalog — owns the connection and registered views.

Two views over the same scan:

- ``ohlcv_raw``: every stored bar, hive columns (``symbol``/``year``/``month``)
  extracted by ``hive_partitioning=true``. Includes phantom post-market bars.
- ``ohlcv``: default analytical surface — bars inside the 09:15–15:30 IST
  session only, matching ``ParquetStorage.read(strip_post_market=True)``.

The store's ``timestamp`` column is tz-naive IST wall time
(``parquet_storage.py`` strips tz on write), so every comparison here uses
naive ``TIME``/``TIMESTAMP`` literals. Never introduce TIMESTAMPTZ.
"""

from __future__ import annotations

import hashlib
import threading
from datetime import datetime
from pathlib import Path

import duckdb

from duck_analytics.config import MARKET_CLOSE, MARKET_OPEN, AnalyticsConfig


class DuckDBCatalog:
    """Lazy, read-only DuckDB connection with the ohlcv views registered.

    Usage::

        cat = DuckDBCatalog()
        con = cat.connection          # registers views on first access
        con.execute("SELECT count(*) FROM ohlcv WHERE symbol='RELIANCE'")
    """

    def __init__(self, config: AnalyticsConfig | None = None) -> None:
        self._config = config or AnalyticsConfig()
        self._con: duckdb.DuckDBPyConnection | None = None
        self._lock = threading.RLock()
        self._fingerprint: str | None = None

    @property
    def execution_lock(self) -> threading.RLock:
        return self._lock

    @property
    def dataset_fingerprint(self) -> str:
        """Stable identity for the configured input files and policy."""
        if self._fingerprint is None:
            digest = hashlib.sha256()
            digest.update(str(Path(self._config.glob).resolve()).encode())
            digest.update(f"|{MARKET_OPEN}|{MARKET_CLOSE}|IST".encode())
            for path in sorted(Path(self._config.base_path).glob("**/data.parquet")):
                stat = path.stat()
                digest.update(f"|{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode())
            self._fingerprint = digest.hexdigest()
        return self._fingerprint

    # ------------------------------------------------------------------ setup

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        """Connection with views registered; created on first access."""
        with self._lock:
            if self._con is None:
                con = duckdb.connect(":memory:")
                con.execute(f"SET memory_limit='{self._config.memory_limit}'")
                con.execute(f"SET threads={self._config.threads}")
                self._register_views(con)
                self._con = con
            return self._con

    def _register_views(self, con: duckdb.DuckDBPyConnection) -> None:
        glob = self._config.glob.replace("'", "''")
        session_where = (
            f"CAST(timestamp AS TIME) >= TIME '{MARKET_OPEN}' "
            f"AND CAST(timestamp AS TIME) <= TIME '{MARKET_CLOSE}'"
        )
        con.execute(
            f"CREATE OR REPLACE VIEW ohlcv_raw AS "
            f"SELECT *, CAST(timestamp AS TIMESTAMP) AS ts "
            f"FROM read_parquet('{glob}', hive_partitioning=true)"
        )
        con.execute(
            f"CREATE OR REPLACE VIEW ohlcv AS "
            f"SELECT * FROM ohlcv_raw WHERE {session_where}"
        )

    # ------------------------------------------------------------------ meta

    def list_symbols(self) -> list[str]:
        """Distinct symbols present in any partition."""
        rows = self.connection.execute(
            "SELECT DISTINCT symbol FROM ohlcv_raw ORDER BY symbol"
        ).fetchall()
        return [r[0] for r in rows]

    def date_range(self, symbol: str) -> tuple[datetime, datetime] | None:
        """Min/max timestamp for a symbol (raw view, includes post-market)."""
        rows = self.connection.execute(
            "SELECT min(ts), max(ts) FROM ohlcv_raw WHERE symbol = ?",
            [symbol],
        ).fetchall()
        lo, hi = rows[0]
        if lo is None or hi is None:
            return None
        return lo, hi

    def schema_of(self, view: str = "ohlcv") -> list[tuple[str, str]]:
        """Column name/type pairs for a registered view."""
        if view not in ("ohlcv", "ohlcv_raw"):
            raise ValueError(f"unknown view {view!r}")
        rows = self.connection.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position",
            [view],
        ).fetchall()
        return [(str(name), str(dtype)) for name, dtype in rows]

    def close(self) -> None:
        with self._lock:
            if self._con is not None:
                self._con.close()
                self._con = None

    def __enter__(self) -> DuckDBCatalog:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def default_config_for(repo_hint: Path | None = None) -> AnalyticsConfig:
    """Config resolved against *repo_hint* so MCP can start from any cwd."""
    cfg = AnalyticsConfig()
    if repo_hint is not None:
        cfg = cfg.resolve_base(repo_hint)
    return cfg


__all__ = ["DuckDBCatalog", "default_config_for"]
