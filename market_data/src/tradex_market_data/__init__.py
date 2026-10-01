"""Datalake module — data catalog and quality management."""

from tradex_market_data.catalog import DataCatalog
from tradex_market_data.corporate_actions import CorporateAction, CorporateActionStore
from tradex_market_data.gap_detector import GapDetector

# ParquetMarketProvider is deprecated but kept for backward compatibility
# and for BulkPrefetchMarketProvider (scanner-specific).
# Import it explicitly so it's available but not in __all__.
from tradex_market_data.market_provider import ParquetMarketProvider  # noqa: F401
from tradex_market_data.parallel_fetcher import ParallelHistoryFetcher
from tradex_market_data.parquet_storage import ParquetStorage
from tradex_market_data.simple_sync import (
    SyncResult,
    series_to_frame,
    simple_sync,
)
from tradex_market_data.symbol_resolve import ResolveResult, resolve_universe_symbols
from tradex_market_data.universe import available_universes, load_universe
from tradex_market_data.window_loader import MarketWindowLoader

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
    "MarketWindowLoader",
]
