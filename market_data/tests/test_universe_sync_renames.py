# market_data/tests/test_universe_sync_renames.py
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from tradex_domain.enums import ExchangeId
from tradex_domain.universe import Equity
from tradex_domain.value_objects import InstrumentId
from tradex_market_data.universe_sync import RenameCandidate, UniverseSyncManager


def make_equity(symbol: str, isin: str | None = None) -> Equity:
    from tradex_domain.instruments import InstrumentMeta
    return Equity(
        instrument_id=InstrumentId.equity("NSE", symbol),
        symbol=symbol,
        exchange=ExchangeId.NSE,
        meta=InstrumentMeta(isin=isin),
    )


def test_detect_rename_same_isin_different_symbol():
    # Mock broker with HEG→HEGAM rename
    broker = MagicMock()
    registry = MagicMock()

    # Old symbol HEG in universe with ISIN
    old_inst = make_equity("HEG", isin="INE545A01024")

    # New symbol HEGAM in broker master with same ISIN
    new_iid = MagicMock()
    registry.provider_key.return_value = "NSE_EQ|INE545A01024"
    registry.meta.return_value = {"isin": "INE545A01024"}

    # Mock loaded instruments
    new_inst = make_equity("HEGAM")
    broker._loaded_instruments = [new_inst]
    broker.registry = registry

    # Setup datalake with old symbol directory
    with tempfile.TemporaryDirectory() as tmp_dir:
        datalake_root = Path(tmp_dir)
        ohlcv_dir = datalake_root / "ohlcv"
        ohlcv_dir.mkdir()
        (ohlcv_dir / "symbol=HEG").mkdir()  # Old symbol has data

        manager = UniverseSyncManager(broker=broker, store=None, datalake_root=datalake_root)
        manager._universe_instruments = [old_inst]  # type: ignore[attr-defined]

        candidates = manager.detect_renames()

        assert len(candidates) == 1
        assert candidates[0].old_symbol == "HEG"
        assert candidates[0].new_symbol == "HEGAM"
        assert candidates[0].isin == "INE545A01024"
        assert candidates[0].confidence == 1.0
        assert candidates[0].status == "pending"


def test_detect_rename_conflict_both_have_data():
    """Test conflict scenario where both old and new symbols have datalake data."""
    broker = MagicMock()
    registry = MagicMock()

    # Old symbol HEG in universe with ISIN
    old_inst = make_equity("HEG", isin="INE545A01024")

    # New symbol HEGAM in broker master with same ISIN
    new_iid = MagicMock()
    registry.provider_key.return_value = "NSE_EQ|INE545A01024"
    registry.meta.return_value = {"isin": "INE545A01024"}

    # Mock loaded instruments
    new_inst = make_equity("HEGAM")
    broker._loaded_instruments = [new_inst]
    broker.registry = registry

    # Setup datalake with BOTH old and new symbol directories (conflict)
    with tempfile.TemporaryDirectory() as tmp_dir:
        datalake_root = Path(tmp_dir)
        ohlcv_dir = datalake_root / "ohlcv"
        ohlcv_dir.mkdir()
        (ohlcv_dir / "symbol=HEG").mkdir()   # Old symbol has data
        (ohlcv_dir / "symbol=HEGAM").mkdir() # New symbol also has data (conflict)

        manager = UniverseSyncManager(broker=broker, store=None, datalake_root=datalake_root)
        manager._universe_instruments = [old_inst]  # type: ignore[attr-defined]

        candidates = manager.detect_renames()

        # Should return empty list due to conflict (both exist)
        assert len(candidates) == 0


def test_detect_rename_old_symbol_not_in_datalake():
    """Test that rename is skipped when old symbol doesn't exist in datalake."""
    broker = MagicMock()
    registry = MagicMock()

    # Old symbol HEG in universe with ISIN
    old_inst = make_equity("HEG", isin="INE545A01024")

    # New symbol HEGAM in broker master with same ISIN
    new_iid = MagicMock()
    registry.provider_key.return_value = "NSE_EQ|INE545A01024"
    registry.meta.return_value = {"isin": "INE545A01024"}

    # Mock loaded instruments
    new_inst = make_equity("HEGAM")
    broker._loaded_instruments = [new_inst]
    broker.registry = registry

    # Setup datalake WITHOUT old symbol directory
    with tempfile.TemporaryDirectory() as tmp_dir:
        datalake_root = Path(tmp_dir)
        ohlcv_dir = datalake_root / "ohlcv"
        ohlcv_dir.mkdir()
        # No symbol=HEG directory exists

        manager = UniverseSyncManager(broker=broker, store=None, datalake_root=datalake_root)
        manager._universe_instruments = [old_inst]  # type: ignore[attr-defined]

        candidates = manager.detect_renames()

        # Should return empty list since old symbol has no data
        assert len(candidates) == 0


def test_detect_rename_no_broker():
    """Test that rename detection returns empty list when no broker is available."""
    # Setup datalake
    with tempfile.TemporaryDirectory() as tmp_dir:
        datalake_root = Path(tmp_dir)
        ohlcv_dir = datalake_root / "ohlcv"
        ohlcv_dir.mkdir()

        manager = UniverseSyncManager(broker=None, store=None, datalake_root=datalake_root)
        manager._universe_instruments = [make_equity("HEG", isin="INE545A01024")]  # type: ignore[attr-defined]

        candidates = manager.detect_renames()

        # Should return empty list when broker is None
        assert len(candidates) == 0
