"""Architecture guard for the canonical parquet history seam."""

from __future__ import annotations

from pathlib import Path


ROUTES = Path(__file__).parents[1] / "trading" / "src" / "tradex_trading" / "interface" / "routes"
MIGRATED = ("chart.py", "stream.py", "stream_indicators.py")


def test_migrated_routes_do_not_rebuild_parquet_history_pipeline():
    sources = {name: (ROUTES / name).read_text() for name in MIGRATED}

    assert all("candles_from_dataframe" not in source for source in sources.values())
    assert all("HistoricalSeries(" not in source for source in sources.values())
    assert all(".read(" not in source for source in sources.values())


def test_migrated_routes_call_the_shared_provider():
    sources = {name: (ROUTES / name).read_text() for name in MIGRATED}

    assert "ParquetMarketProvider" in sources["chart.py"]
    assert "ParquetMarketProvider" in sources["stream.py"]
    assert "ParquetMarketProvider" in sources["stream_indicators.py"]
