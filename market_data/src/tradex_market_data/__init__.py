"""Datalake module — data catalog and quality management."""

from tradex_market_data.catalog import DataCatalog
from tradex_market_data.corporate_actions import CorporateAction, CorporateActionStore
from tradex_market_data.gap_detector import GapDetector
from tradex_market_data.market_provider import ParquetMarketProvider
from tradex_market_data.parallel_fetcher import ParallelHistoryFetcher
from tradex_market_data.parquet_storage import ParquetStorage
from tradex_market_data.simple_sync import (
    SyncResult,
    series_to_frame,
    simple_sync,
)
from tradex_market_data.symbol_resolve import ResolveResult, resolve_universe_symbols
from tradex_market_data.universe import available_universes, load_universe

# ParquetBacktestLoader imports replay/strategy — keep it lazy so scanner
# discovery can load universes without a circular import.


def __getattr__(name: str):
    if name == "ParquetBacktestLoader":
        from tradex_market_data.backtest_loader import ParquetBacktestLoader

        return ParquetBacktestLoader
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DataCatalog",
    "CorporateAction",
    "CorporateActionStore",
    "ParallelHistoryFetcher",
    "ParquetStorage",
    "GapDetector",
    "simple_sync",
    "series_to_frame",
    "SyncResult",
    "ResolveResult",
    "resolve_universe_symbols",
    "load_universe",
    "available_universes",
    "ParquetMarketProvider",
    "ParquetBacktestLoader",
]
