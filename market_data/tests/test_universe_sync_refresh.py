# market_data/tests/test_universe_sync_refresh.py
import tempfile
from pathlib import Path
from unittest.mock import patch

from tradex_domain.enums import ExchangeId
from tradex_domain.universe import Equity
from tradex_domain.value_objects import InstrumentId
from tradex_market_data.universe_sync import UniverseDelta, UniverseSyncManager


def make_equity(symbol: str) -> Equity:
    return Equity(
        instrument_id=InstrumentId.equity("NSE", symbol),
        symbol=symbol,
        exchange=ExchangeId.NSE,
    )


def test_refresh_universe_detects_added_symbols(tmp_path):
    # Setup: datalake has 2 symbols, CSV has 3
    datalake = tmp_path / "data" / "ohlcv"
    datalake.mkdir(parents=True)
    (datalake / "symbol=SYM1").mkdir()
    (datalake / "symbol=SYM2").mkdir()

    # Mock CSV with 3 symbols
    csv_symbols = [make_equity("SYM1"), make_equity("SYM2"), make_equity("SYM3")]

    with patch("tradex_market_data.universe_sync.load_universe", return_value=csv_symbols):
        manager = UniverseSyncManager(broker=None, store=None, csv_dir=tmp_path)
        manager._datalake_root = datalake.parent  # Point to parent of ohlcv

        delta = manager.refresh_universe("nifty500")

        assert len(delta.added) == 1
        assert delta.added[0].symbol == "SYM3"
        assert len(delta.removed) == 0
        assert len(delta.unchanged) == 2
