"""ParquetStorage — Hive-partitioned Parquet store with upsert semantics.

Layout: ``base_path/ohlcv/symbol={SYMBOL}/year={YYYY}/month={MM}/data.parquet``

Partition pruning keeps reads fast: ``read(symbols, start, end)`` only scans
the symbol + year/month partitions that overlap the requested range.

Upsert is idempotent: re-upserting the same (symbol, timeframe, timestamp)
replaces old rows instead of duplicating them.

Adapted from nTrade's ParquetStorage.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

# ponytail: pyarrow is already installed (ParquetDataCatalog depends on it).
import pyarrow as pa
import pyarrow.parquet as pq
from tradex_domain.market_calendar import MARKET_CLOSE, MARKET_OPEN

_BASE_COLUMNS = [
    "symbol", "exchange", "kind", "timeframe", "timestamp",
    "open", "high", "low", "close", "volume",
]

# ponytail: calendar constants single-sourced (domain/market_calendar.py); keep
# local aliases so this module's grep for _MARKET_OPEN still hits.
_MARKET_OPEN = MARKET_OPEN
_MARKET_CLOSE = MARKET_CLOSE


class ParquetStorage:
    """Hive-partitioned Parquet store for OHLCV history.

    ``base_path`` is the root directory; the store creates
    ``base_path/ohlcv/symbol=.../year=.../month=.../`` as needed.
    """

    def __init__(self, base_path: str | Path):
        self.base_path = Path(base_path)
        # ponytail: if base_path already ends with 'ohlcv', use it directly
        if self.base_path.name == "ohlcv":
            self._ohlcv_root = self.base_path
        else:
            self._ohlcv_root = self.base_path / "ohlcv"
        self._ohlcv_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ write

    def upsert(self, df: pd.DataFrame) -> int:
        """Insert-or-replace rows by (symbol, timeframe, timestamp).

        Returns the number of rows written (post-dedup).
        """
        if df is None or df.empty:
            return 0

        df = self._prepare_frame(df)
        written = 0

        for symbol in df["symbol"].unique():
            sub = df[df["symbol"] == symbol]
            for (year, month), grp in sub.groupby(
                [sub["timestamp"].dt.year, sub["timestamp"].dt.month]
            ):
                partition_dir = (
                    self._ohlcv_root
                    / f"symbol={symbol}"
                    / f"year={year}"
                    / f"month={int(month):02d}"
                )
                partition_dir.mkdir(parents=True, exist_ok=True)
                parquet_file = partition_dir / "data.parquet"

                if parquet_file.exists():
                    existing = pq.ParquetFile(parquet_file).read().to_pandas()
                    existing["timestamp"] = pd.to_datetime(existing["timestamp"])
                    # ponytail: normalize tz to match incoming data
                    if getattr(existing["timestamp"].dt, "tz", None) is not None:
                        existing["timestamp"] = existing["timestamp"].dt.tz_localize(None)
                    key_cols = ["symbol", "timeframe", "timestamp"]
                    existing = existing[
                        ~existing.set_index(key_cols).index.isin(
                            grp.set_index(key_cols).index
                        )
                    ]
                    if not existing.empty:
                        self._write_parquet(existing, parquet_file)
                    else:
                        parquet_file.unlink()

                to_write = grp
                if not to_write.empty:
                    self._write_parquet(to_write, parquet_file)
                    written += len(to_write)

        return written

    def _write_parquet(self, df: pd.DataFrame, path: Path) -> None:
        """Write a DataFrame to parquet (append + dedupe if file exists)."""
        if path.exists():
            existing = pq.ParquetFile(path).read().to_pandas()
            existing["timestamp"] = pd.to_datetime(existing["timestamp"])
            if getattr(existing["timestamp"].dt, "tz", None) is not None:
                existing["timestamp"] = existing["timestamp"].dt.tz_localize(None)
            combined = pd.concat([existing, df], ignore_index=True)
            combined = combined.drop_duplicates(
                subset=["symbol", "timeframe", "timestamp"], keep="last"
            )
            table = pa.Table.from_pandas(combined, preserve_index=False)
            pq.write_table(table, str(path), use_dictionary=False)
        else:
            table = pa.Table.from_pandas(df, preserve_index=False)
            pq.write_table(table, str(path), use_dictionary=False)

    def _prepare_frame(self, df: pd.DataFrame) -> pd.DataFrame:
        """Ensure required columns + types for parquet storage."""
        df = df.copy()  # ponytail: never mutate caller's DataFrame
        for col in _BASE_COLUMNS:
            if col not in df.columns:
                df[col] = None
        df = df[_BASE_COLUMNS + [c for c in df.columns if c not in _BASE_COLUMNS]].copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        # ponytail: strip tz — storage contract is tz-naive wall time
        if getattr(df["timestamp"].dt, "tz", None) is not None:
            df["timestamp"] = df["timestamp"].dt.tz_localize(None)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["volume"] = df["volume"].fillna(0).astype("int64")
        return df

    # ------------------------------------------------------------------ read

    def read(
        self,
        symbols: list[str] | None = None,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        timeframe: str | None = None,
        strip_post_market: bool = True,
    ) -> pd.DataFrame:
        """Read OHLCV data with partition pruning.

        Parameters
        ----------
        strip_post_market : bool
            Drop bars outside the 09:15-15:30 IST market session (default
            True). Dhan emits phantom post-market bars up to 20:00; pass
            ``False`` to read the raw stored bars (e.g. for audit).
        """
        if symbols is not None and len(symbols) == 0:
            return pd.DataFrame(columns=_BASE_COLUMNS)

        start_ts = pd.Timestamp(start) if start is not None else None
        end_ts = pd.Timestamp(end) if end is not None else None
        # ponytail: normalize tz-aware inputs to naive
        if start_ts is not None and start_ts.tzinfo is not None:
            start_ts = start_ts.tz_localize(None)
        if end_ts is not None and end_ts.tzinfo is not None:
            end_ts = end_ts.tz_localize(None)

        hive_paths = self._resolve_partitions(symbols, start_ts, end_ts)
        if not hive_paths:
            return pd.DataFrame(columns=_BASE_COLUMNS)

        tables = []
        for pdir in hive_paths:
            parquet_file = pdir / "data.parquet"
            if not parquet_file.exists():
                continue
            table = pq.ParquetFile(parquet_file).read()
            if table.num_rows > 0:
                tables.append(table.to_pandas())

        if not tables:
            return pd.DataFrame(columns=_BASE_COLUMNS)

        result = pd.concat(tables, ignore_index=True)
        result["timestamp"] = pd.to_datetime(result["timestamp"])
        if getattr(result["timestamp"].dt, "tz", None) is not None:
            result["timestamp"] = result["timestamp"].dt.tz_localize(None)
        if start_ts is not None:
            result = result[result["timestamp"] >= start_ts]
        if end_ts is not None:
            result = result[result["timestamp"] <= end_ts]
        if timeframe is not None:
            result = result[result["timeframe"] == timeframe]
        if strip_post_market:
            result = result[
                (result["timestamp"].dt.time >= _MARKET_OPEN)
                & (result["timestamp"].dt.time <= _MARKET_CLOSE)
            ]
        return result.sort_values(["symbol", "timestamp"]).reset_index(drop=True)

    def _resolve_partitions(
        self,
        symbols: list[str] | None,
        start_ts: pd.Timestamp | None,
        end_ts: pd.Timestamp | None,
    ) -> list[Path]:
        """Resolve which Hive partition dirs to scan."""
        hive_paths: list[Path] = []
        if start_ts is not None and end_ts is not None:
            for sym in symbols or self._all_symbols():
                for period in pd.period_range(start=start_ts, end=end_ts, freq="M"):
                    pdir = (
                        self._ohlcv_root
                        / f"symbol={sym}"
                        / f"year={period.year}"
                        / f"month={period.month:02d}"
                    )
                    if pdir.exists():
                        hive_paths.append(pdir)
        elif symbols is not None:
            for sym in symbols:
                sym_dir = self._ohlcv_root / f"symbol={sym}"
                if sym_dir.exists():
                    hive_paths.extend(p for p in sym_dir.rglob("month=*") if p.is_dir())
        else:
            hive_paths = list(self._ohlcv_root.rglob("month=*"))
        return hive_paths

    # ------------------------------------------------------------------ metadata

    def symbols(self) -> list[str]:
        """All symbols present in the store."""
        return self._all_symbols()

    def _all_symbols(self) -> list[str]:
        if not self._ohlcv_root.exists():
            return []
        return sorted(
            p.name.split("=", 1)[1]
            for p in self._ohlcv_root.iterdir()
            if p.is_dir() and p.name.startswith("symbol=")
        )

    def date_range(
        self, symbol: str, timeframe: str | None = None
    ) -> tuple[datetime, datetime] | None:
        """Min/max timestamp for a symbol."""
        df = self.read(symbols=[symbol], timeframe=timeframe)
        if df.empty:
            return None
        return df["timestamp"].min(), df["timestamp"].max()

    def clear(self) -> None:
        """Wipe all stored data."""
        if self._ohlcv_root.exists():
            shutil.rmtree(self._ohlcv_root)
        self._ohlcv_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------- DuckDB integration

    def duckdb_scan(self, con, *, start=None, end=None) -> str:
        """Register a DuckDB view over the parquet store.

        Usage::

            import duckdb
            store = ParquetStorage("data/")
            con = duckdb.connect()
            store.duckdb_scan(con)
            con.execute("SELECT * FROM ohlcv WHERE symbol='RELIANCE'").fetchdf()
        """
        path_pattern = str(self._ohlcv_root / "**" / "data.parquet")
        if start is None and end is None:
            now = pd.Timestamp.now()
            start = now - pd.Timedelta(minutes=1)
            end = now
        where = ""
        if start is not None or end is not None:
            parts = []
            if start is not None:
                ts_val = pd.Timestamp(start)
                if ts_val.tzinfo is not None:
                    ts_val = ts_val.tz_localize(None)
                parts.append(f"timestamp >= TIMESTAMP '{ts_val.strftime('%Y-%m-%d %H:%M:%S')}'")
            if end is not None:
                ts_val = pd.Timestamp(end)
                if ts_val.tzinfo is not None:
                    ts_val = ts_val.tz_localize(None)
                parts.append(f"timestamp <= TIMESTAMP '{ts_val.strftime('%Y-%m-%d %H:%M:%S')}'")
            where = " WHERE " + " AND ".join(parts)
        con.execute(
            f"CREATE OR REPLACE VIEW ohlcv AS "
            f"SELECT *, CAST(timestamp AS TIMESTAMP) AS ts "
            f"FROM parquet_scan('{path_pattern}', hive_partitioning=True){where}"
        )
        return "ohlcv"


__all__ = ["ParquetStorage"]
