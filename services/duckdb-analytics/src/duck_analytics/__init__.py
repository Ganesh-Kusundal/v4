"""duck_analytics — read-only DuckDB analytics over the OHLCV parquet datalake.

Query-based analytics layer: registered views over the Hive-partitioned
parquet store (``data/ohlcv/symbol=…/year=…/month=…/data.parquet``), guarded
query execution, canned scanner templates, and an optional stdio MCP server.

Isolated by design: core depends only on duckdb + pyarrow. Optional
analytics tools (patterns, testing fixtures) require pandas + numpy
(install the ``analytics`` extra). It never imports tradex_trading /
tradex_brokers / tradex_domain.
"""

from duck_analytics.catalog import DuckDBCatalog
from duck_analytics.config import AnalyticsConfig

__all__ = ["AnalyticsConfig", "DuckDBCatalog"]
