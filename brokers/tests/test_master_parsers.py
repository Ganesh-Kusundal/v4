"""Broker-package instrument-master parsers (moved out of trading/runtime/live).

``tradex_brokers.dhan.master`` / ``tradex_brokers.upstox.master`` own the
provider wire-format parsing; trading only orchestrates the cache. These tests
lock the parser contract so trading never needs to re-implement it.
"""

from __future__ import annotations

import gzip
import json

import pytest
from tradex_domain.errors import SDKError

from tradex_brokers.dhan.instruments import SEGMENT_CANONICAL as DHAN_SEGMENT_CANONICAL
from tradex_brokers.dhan.master import (
    MIN_MASTER_ROWS,
    dhan_master_download,
    parse_dhan_master,
)
from tradex_brokers.upstox.instruments import SEGMENT_CANONICAL as UPSTOX_SEGMENT_CANONICAL
from tradex_brokers.upstox.master import (
    parse_upstox_master,
    upstox_master_download,
)


class TestDhanMasterParser:
    def test_parse_csv_equity_row(self) -> None:
        csv_text = (
            "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_TRADING_SYMBOL,SEM_SMST_SECURITY_ID,"
            "SEM_INSTRUMENT_NAME\n"
            "NSE,E,RELIANCE,2885,EQ\n"
        )
        rows = parse_dhan_master(csv_text.encode(), strict=False)
        assert len(rows) == 1
        row = rows[0]
        assert row["symbol"] == "RELIANCE"
        assert row["exchange"] == "NSE"
        assert row["key"] == "NSE:2885"
        assert row["asset_class"] == "EQUITY"

    def test_parse_csv_derivatives_structure(self) -> None:
        csv_text = (
            "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_TRADING_SYMBOL,SEM_SMST_SECURITY_ID,"
            "SEM_INSTRUMENT_NAME,SEM_OPTION_TYPE,SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,"
            "SM_SYMBOL_NAME,SEM_CUSTOM_SYMBOL\n"
            "MCX,M,SILVER-04Sep2026-FUT,510000,FUTCOM,,2026-09-04 23:30:00,,SILVER,"
            "SILVER 04 SEP\n"
            "MCX,M,SILVERM-24Aug2026-279000-CE,509665,OPTFUT,CE,2026-08-24 23:30:00,"
            "279000.00000,SILVERM,SILVERM 24 AUG 279000 CALL\n"
        )
        rows = parse_dhan_master(csv_text.encode(), strict=False)
        fut = next(r for r in rows if r["instrument_type"] == "FUTCOM")
        assert fut["right"] is None
        assert fut["expiry"] == "2026-09-04"
        assert fut["underlying"] == "SILVER"
        opt = next(r for r in rows if r["instrument_type"] == "OPTFUT")
        assert opt["right"] == "CE"
        assert opt["strike"] == "279000.00000"
        assert opt["underlying"] == "SILVERM"

    def test_parse_derivatives_asset_class_and_contract_meta(self) -> None:
        """Derivative rows carry the real asset_class (not OTHER) plus the
        contract metadata (lot/tick) the quant layer consumes."""
        csv_text = (
            "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_TRADING_SYMBOL,SEM_SMST_SECURITY_ID,"
            "SEM_INSTRUMENT_NAME,SEM_OPTION_TYPE,SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,"
            "SM_SYMBOL_NAME,SEM_LOT_UNITS,SEM_TICK_SIZE\n"
            "MCX,M,SILVER-04Sep2026-FUT,510000,FUTCOM,,2026-09-04 23:30:00,,SILVER,30,0.5\n"
            "MCX,M,SILVERM-24Aug2026-279000-CE,509665,OPTFUT,CE,2026-08-24 23:30:00,"
            "279000.00000,SILVERM,250,0.1\n"
        )
        rows = parse_dhan_master(csv_text.encode(), strict=False)
        fut = next(r for r in rows if r["instrument_type"] == "FUTCOM")
        assert fut["asset_class"] == "FUTURE"
        assert fut["lot_size"] == "30"
        assert fut["tick_size"] == "0.5"
        opt = next(r for r in rows if r["instrument_type"] == "OPTFUT")
        assert opt["asset_class"] == "OPTION"
        assert opt["lot_size"] == "250"

    def test_underlying_root_prefers_trading_symbol_over_display_name(self) -> None:
        """Currency rows whose SM_SYMBOL_NAME is empty use the trading-symbol
        root, never the human display name (87k-row audit regression)."""
        csv_text = (
            "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_TRADING_SYMBOL,SEM_SMST_SECURITY_ID,"
            "SEM_INSTRUMENT_NAME,SEM_OPTION_TYPE,SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,"
            "SM_SYMBOL_NAME,SEM_CUSTOM_SYMBOL\n"
            "NSE,C,EURINR-Mar2026-104.75-CE,80891,OPTCUR,CE,2026-03-27 23:30:00,"
            "104.75000,,EURINR 27 MAR 104.75 CALL\n"
        )
        rows = parse_dhan_master(csv_text.encode(), strict=False)
        opt = rows[0]
        assert opt["underlying"] == "EURINR"

    def test_segment_map_is_single_source(self) -> None:
        # MCX/M -> MCX used by the parser is the exported public map.
        assert DHAN_SEGMENT_CANONICAL[("MCX", "M")] == "MCX"
        assert DHAN_SEGMENT_CANONICAL[("NSE", "D")] == "NFO"

    def test_strict_rejects_small_master(self) -> None:
        csv_text = "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_TRADING_SYMBOL,SEM_SMST_SECURITY_ID\n"
        with pytest.raises(SDKError, match="too few rows"):
            parse_dhan_master(csv_text.encode(), strict=True)

    def test_empty_bytes(self) -> None:
        assert parse_dhan_master(b"", strict=False) == []

    def test_downloader_returns_body(self) -> None:
        calls = []

        def fetch(method: str, url: str, **kwargs: object) -> tuple[int, object]:
            calls.append((method, url))
            return 200, b"csv-body"

        download = dhan_master_download(fetch, strict=True)
        assert download() == b"csv-body"
        assert calls[0][0] == "GET"

    def test_downloader_strict_raises_on_bad_status(self) -> None:
        def fetch(*_args: object, **_kwargs: object) -> tuple[int, object]:
            return 500, b"error"

        download = dhan_master_download(fetch, strict=True)
        with pytest.raises(SDKError, match="HTTP 500"):
            download()

    def test_downloader_non_strict_empty_on_bad_status(self) -> None:
        def fetch(*_args: object, **_kwargs: object) -> tuple[int, object]:
            return 500, b"error"

        download = dhan_master_download(fetch, strict=False)
        assert download() == b""


class TestUpstoxMasterParser:
    def test_parse_gzip_json(self) -> None:
        rows = [
            {
                "exchange": "NSE",
                "trading_symbol": "RELIANCE",
                "instrument_key": "NSE_EQ|2885",
                "instrument_type": "EQ",
                "segment": "NSE_EQ",
            }
        ]
        raw = gzip.compress(json.dumps(rows).encode("utf-8"))
        parsed = parse_upstox_master(raw, strict=False)
        assert len(parsed) == 1
        assert parsed[0]["exchange"] == "NSE"
        assert parsed[0]["key"] == "NSE_EQ|2885"

    def test_parse_derivatives_structure(self) -> None:
        rows = [
            {
                "exchange": "MCX",
                "trading_symbol": "GOLD 120000 CE 30 OCT 26",
                "instrument_key": "MCX_FO|579316",
                "instrument_type": "CE",
                "expiry": 1793384999000,
                "strike_price": 120000.0,
                "underlying_symbol": "GOLD",
                "segment": "MCX_FO",
            }
        ]
        result = parse_upstox_master(rows, strict=False)
        assert len(result) == 1
        opt = result[0]
        assert opt["right"] == "CE"
        assert opt["expiry"] == "2026-10-30"
        assert opt["strike"] == 120000.0
        assert opt["underlying"] == "GOLD"

    def test_parse_derivatives_asset_class_and_lot(self) -> None:
        """Upstox derivative rows carry the real asset_class and lot_size."""
        rows = [
            {
                "exchange": "MCX",
                "trading_symbol": "GOLD 120000 CE 30 OCT 26",
                "instrument_key": "MCX_FO|579316",
                "instrument_type": "CE",
                "expiry": 1793384999000,
                "strike_price": 120000.0,
                "underlying_symbol": "GOLD",
                "segment": "MCX_FO",
                "lot_size": "1",
            }
        ]
        result = parse_upstox_master(rows, strict=False)
        opt = result[0]
        assert opt["asset_class"] == "OPTION"
        assert opt["lot_size"] == "1"

    def test_segment_canonical_maps(self) -> None:
        assert UPSTOX_SEGMENT_CANONICAL["NSE_FO"] == "NFO"
        assert UPSTOX_SEGMENT_CANONICAL["MCX_FO"] == "MCX"
        assert UPSTOX_SEGMENT_CANONICAL["NSE_COM"] == "NSE_COMM"
        assert UPSTOX_SEGMENT_CANONICAL["NCD_FO"] == "CDS"

    def test_invalid_exchange_rows_dropped(self) -> None:
        rows = [
            {"exchange": "GLOBAL", "trading_symbol": "SOMETHING", "segment": "NSE_EQ"},
            {"exchange": "NSE", "trading_symbol": "RELIANCE", "segment": "NSE_EQ"},
        ]
        result = parse_upstox_master(rows, strict=False)
        assert len(result) == 1
        assert result[0]["symbol"] == "RELIANCE"

    def test_strict_rejects_small_master(self) -> None:
        rows = [{"exchange": "NSE", "trading_symbol": "RELIANCE", "segment": "NSE_EQ"}]
        with pytest.raises(SDKError, match="too few rows"):
            parse_upstox_master(rows, strict=True)

    def test_empty_bytes(self) -> None:
        assert parse_upstox_master(b"", strict=False) == []

    def test_downloader_returns_body(self) -> None:
        calls = []

        def fetch(method: str, url: str, **kwargs: object) -> tuple[int, object]:
            calls.append((method, url))
            return 200, b"gz-body"

        download = upstox_master_download(fetch, strict=True)
        assert download() == b"gz-body"
        assert calls[0][0] == "GET"

    def test_downloader_strict_raises_on_bad_status(self) -> None:
        def fetch(*_args: object, **_kwargs: object) -> tuple[int, object]:
            return 502, b"error"

        download = upstox_master_download(fetch, strict=True)
        with pytest.raises(SDKError, match="HTTP 502"):
            download()


def test_min_rows_constant_shared() -> None:
    assert MIN_MASTER_ROWS == 10_000
