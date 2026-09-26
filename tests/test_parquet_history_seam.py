"""Architecture guard for the canonical parquet history seam."""

from __future__ import annotations

import re
from pathlib import Path


ROUTES = Path(__file__).parents[1] / "interfaces" / "src" / "tradex_interfaces" / "routes"
MIGRATED = ("chart.py", "stream.py", "stream_indicators.py")

#: The symbol-search endpoint lists instruments from the store's catalog. That
#: is metadata, not an OHLCV history pipeline, so it is the one direct store
#: read these routes are allowed to make. ``symbols`` must be the only argument,
#: so a read that also takes a time range cannot hide behind this allowance.
_CATALOG_READ = re.compile(r"\.read\(symbols=[A-Za-z_][A-Za-z0-9_]*\)")


def test_migrated_routes_do_not_rebuild_parquet_history_pipeline():
    sources = {name: (ROUTES / name).read_text() for name in MIGRATED}

    assert all("candles_from_dataframe" not in source for source in sources.values())
    assert all("HistoricalSeries(" not in source for source in sources.values())
    # History must be fetched through the provider, never by reading the store
    # directly. The symbol-search endpoint's catalog read is not a history
    # pipeline, so discount exactly that call and require no other ``.read(``.
    assert all(
        ".read(" not in _CATALOG_READ.sub("", source) for source in sources.values()
    )


def test_migrated_routes_call_the_shared_provider():
    sources = {name: (ROUTES / name).read_text() for name in MIGRATED}

    assert "ParquetMarketProvider" in sources["chart.py"]
    assert "ParquetMarketProvider" in sources["stream.py"]
    assert "ParquetMarketProvider" in sources["stream_indicators.py"]
