"""Deterministic synthetic OHLCV lake factory for tests.

Builds Hive-partitioned parquet stores matching the ``ParquetStorage``
layout so analytics behavior can be exercised without broker or trading
dependencies. Shipped inside the package so downstream consumers can reuse
it for their own analytics regression tests.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

BASE_COLUMNS = [
    "symbol", "exchange", "kind", "timeframe", "timestamp",
    "open", "high", "low", "close", "volume",
]

#: Six consecutive NSE trading days (Wed 2026-08-05 .. Wed 2026-08-12).
DAYS = [datetime(2026, 8, d) for d in (5, 6, 7, 10, 11, 12)]


def _as_day(day: datetime) -> datetime:
    return datetime(day.year, day.month, day.day)


def make_bars(symbol: str, day: datetime, minutes: int = 30,
              base_price: float | None = None) -> list[dict]:
    """Deterministic 1m bars for one morning: close mostly walks upward."""
    rows: list[dict] = []
    day_norm = _as_day(day)
    try:
        drift = 8.0 * DAYS.index(day_norm)
    except ValueError:
        # Days past the fixture window keep drifting.
        drift = 8.0 * len(DAYS) + 2.0 * (day_norm - DAYS[-1]).days
    price = base_price if base_price is not None else 100.0 + drift
    for i in range(minutes):
        ts = day.replace(hour=9, minute=15 + i, second=0, microsecond=0)
        price += 1.0 if (i % 7 != 3) else -0.5
        rows.append({
            "symbol": symbol, "exchange": "NSE", "kind": "equity",
            "timeframe": "1m", "timestamp": ts,
            "open": round(price - 0.5, 2), "high": round(price + 0.5, 2),
            "low": round(price - 1.0, 2), "close": round(price, 2),
            "volume": 1000 + 10 * i,
        })
    return rows


def append_rows(root, symbol: str, rows: list[dict]) -> None:
    """Append rows to the symbol/month partition (create if absent)."""
    df_new = pd.DataFrame(rows)[BASE_COLUMNS]
    out = (
        root / f"symbol={symbol}"
        / f"year={rows[0]['timestamp'].year}"
        / f"month={rows[0]['timestamp'].month:02d}" / "data.parquet"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        df_old = pd.read_parquet(out)
        df = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df = df_new
    df.to_parquet(out, index=False)


def write_partition(root, symbol: str, rows: list[dict]) -> None:
    """Alias kept for older call sites — appends like :func:`append_rows`."""
    append_rows(root, symbol, rows)


__all__ = ["BASE_COLUMNS", "DAYS", "append_rows", "make_bars", "write_partition"]
