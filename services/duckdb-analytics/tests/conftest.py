"""Shared fixtures — synthetic Hive parquet store matching ParquetStorage layout."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Self-sufficient import path: running from the repo root (rootdir=v4) must
# work exactly like running from services/duckdb-analytics.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from duck_analytics.catalog import DuckDBCatalog  # noqa: E402
from duck_analytics.config import AnalyticsConfig  # noqa: E402
from duck_analytics.query import QueryService  # noqa: E402
from duck_analytics.testing import DAYS, append_rows, make_bars  # noqa: E402

__all__ = ["DAYS", "lake", "catalog", "service"]


@pytest.fixture()
def lake(tmp_path):
    """Two symbols x six trading days; RELIANCE gets a phantom post-market bar."""
    root = tmp_path / "ohlcv"
    for day in DAYS:
        write_partition(root, "RELIANCE", make_bars("RELIANCE", day))
        write_partition(root, "TCS", make_bars("TCS", day))
    # Phantom bar at 18:00 on day 1 (Dhan-style post-market artifact).
    phantom = {
        "symbol": "RELIANCE", "exchange": "NSE", "kind": "equity",
        "timeframe": "1m", "timestamp": DAYS[0].replace(hour=18),
        "open": 999.0, "high": 999.0, "low": 999.0, "close": 999.0,
        "volume": 1,
    }
    append_rows(root, "RELIANCE", [phantom])
    return root


@pytest.fixture()
def catalog(lake):
    cfg = AnalyticsConfig(base_path=lake)
    cat = DuckDBCatalog(cfg)
    yield cat
    cat.close()


@pytest.fixture()
def service(catalog, lake):
    return QueryService(catalog, AnalyticsConfig(base_path=lake))


def write_partition(root, symbol: str, rows: list[dict]) -> None:
    append_rows(root, symbol, rows)
