"""Integration test: full universe sync pipeline on small test universe."""

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
from tradex_domain.enums import ExchangeId
from tradex_domain.instruments import Equity, InstrumentMeta
from tradex_domain.value_objects import InstrumentId
from tradex_market_data.universe_sync import UniverseSyncManager


def test_sync_all_pipeline_integration():
    """Run all 3 phases on 5-symbol test universe."""
    with tempfile.TemporaryDirectory() as tmpdir:
        datalake = Path(tmpdir) / "data"
        datalake.mkdir()

        # Setup: Create 3 symbols in datalake with dummy parquet files
        for sym in ["SYM1", "SYM2", "SYM3"]:
            sym_dir = datalake / "ohlcv" / f"symbol={sym}"
            sym_dir.mkdir(parents=True)
            # Create a dummy parquet file with symbol column
            df = pd.DataFrame({
                "timestamp": pd.date_range("2024-01-01", periods=10, freq="D"),
                "open": [100.0] * 10,
                "high": [101.0] * 10,
                "low": [99.0] * 10,
                "close": [100.5] * 10,
                "volume": [1000] * 10,
                "symbol": [sym] * 10,
            })
            df.to_parquet(sym_dir / f"{sym}_data.parquet", index=False)

        # Mock broker with one rename (SYM2 → SYM2NEW)
        broker = MagicMock()
        broker.registry.provider_key.return_value = "NSE_EQ|INE123"
        broker.registry.meta.return_value = {"isin": "INE123"}

        # Mock loaded instruments
        sym2_new = Equity(
            instrument_id=InstrumentId.equity("NSE", "SYM2NEW"),
            symbol="SYM2NEW",
            exchange=ExchangeId.NSE,
        )
        broker._loaded_instruments = [sym2_new]

        store = MagicMock()

        # Mock CSV with 4 symbols (SYM1, SYM2, SYM3, SYM4)
        csv_symbols = []
        for i, s in enumerate(["SYM1", "SYM2", "SYM3", "SYM4"]):
            meta = InstrumentMeta(isin="INE123" if s == "SYM2" else None)
            csv_symbols.append(
                Equity(
                    instrument_id=InstrumentId.equity("NSE", s),
                    symbol=s,
                    exchange=ExchangeId.NSE,
                    meta=meta,
                )
            )

        manager = UniverseSyncManager(
            broker=broker,
            store=store,
            datalake_root=datalake,
        )

        with patch("tradex_market_data.universe_sync.load_universe", return_value=csv_symbols):
            report = manager.sync_all("test_universe", skip_retry=True)

        # Verify universe delta
        assert len(report.universe.added) == 1  # SYM4
        assert report.universe.added[0].symbol == "SYM4"
        assert len(report.universe.removed) == 0

        # Verify rename detection
        assert len(report.renames) == 1
        assert report.renames[0].old_symbol == "SYM2"
        assert report.renames[0].new_symbol == "SYM2NEW"

        # Verify migration
        assert len(report.migrations) == 1
        assert report.migrations[0].success

        print("Integration test passed!")
