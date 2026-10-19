"""Architecture guard for the canonical parquet history seam."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

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


def test_the_lake_converter_is_patchable_at_the_seam_production_reads(monkeypatch):
    """Doubles for ``candles_from_dataframe`` must patch ``tradex_shared``.

    ``_to_candles`` resolves the helper off the ``tradex_shared`` package at
    call time, so a double installed on any other module is dead code: the real
    converter runs, raises on the test's fake frame, and the route answers with
    an error frame the test is not waiting for. That is not hypothetical —
    when the helper left the broker layer (3747a86),
    seven replay doubles silently stopped intercepting and 18 tests blocked in
    ``receive_json`` for 353s instead of failing. This asserts the seam, so the
    next move breaks one test here rather than hanging a whole file.
    """
    # Import a helper module FIRST, deliberately: importing before the patch
    # below proves the double is read off ``tradex_shared`` at call time
    # (a stale module-level binding elsewhere cannot mask a seam regression).
    import tradex_market_data.market_provider as _seam_module
    import tradex_shared
    from tradex_market_data.market_provider import ParquetMarketProvider

    seen: list[Any] = []

    def _double(instrument: Any, _frame: Any, **_kwargs: Any) -> list[str]:
        seen.append(instrument)
        return ["sentinel"]

    assert getattr(_seam_module, "candles_from_dataframe", None) is not _double
    monkeypatch.setattr(tradex_shared, "candles_from_dataframe", _double)

    assert ParquetMarketProvider._to_candles("NSE:TEST", object()) == ["sentinel"]
    assert seen == ["NSE:TEST"]
