"""Datalake module — data catalog and quality management."""

from tradex_trading.datalake.backtest_loader import ParquetBacktestLoader
from tradex_trading.datalake.catalog import DataCatalog
from tradex_trading.datalake.corporate_actions import CorporateAction, CorporateActionStore
from tradex_trading.datalake.gap_detector import GapDetector
from tradex_trading.datalake.historical_sync import SyncOrchestrator
from tradex_trading.datalake.market_provider import ParquetMarketProvider
from tradex_trading.datalake.parallel_fetcher import ParallelHistoryFetcher
from tradex_trading.datalake.parquet_storage import ParquetStorage
from tradex_trading.datalake.simple_sync import (
    SyncResult,
    series_to_frame,
    simple_sync,
)
from tradex_trading.datalake.universe import available_universes, load_universe

__all__ = [
    "DataCatalog",
    "CorporateAction",
    "CorporateActionStore",
    "ParallelHistoryFetcher",
    "ParquetStorage",
    "GapDetector",
    "simple_sync",
    "series_to_frame",
    "SyncOrchestrator",
    "SyncResult",
    "load_universe",
    "available_universes",
    "ParquetMarketProvider",
    "ParquetBacktestLoader",
]
