"""Tests for universe ISIN attach + symbol_resolve + sync classify."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from tradex_domain import Equity
from tradex_domain.instruments import InstrumentMeta
from tradex_domain.value_objects import InstrumentId

from tradex_trading.datalake.simple_sync import _classify_fetch_error, simple_sync
from tradex_trading.datalake.symbol_resolve import resolve_universe_symbols
from tradex_trading.datalake.universe import load_universe


def test_universe_attaches_isin_from_csv(tmp_path):
    csv = tmp_path / "nifty50_list.csv"
    csv.write_text(
        "Company Name,Industry,Symbol,Series,ISIN Code\n"
        "Foo Ltd,X,FOO,EQ,INE000A01011\n"
        "Bar Ltd,Y,BAR,BE,INE000B01022\n"
        "Debt Ltd,Z,DEBT,D1,INE000C01033\n"
    )
    insts = load_universe("nifty50", csv_dir=tmp_path)
    by = {i.symbol: i for i in insts}
    assert set(by) == {"FOO", "BAR"}
    assert by["FOO"].meta.isin == "INE000A01011"
    assert by["FOO"].meta.extra.get("series") == "EQ"


def test_classify_fetch_error_kinds():
    assert _classify_fetch_error("NSE:HEG: no provider key for NSE:HEG") == "NO_PROVIDER_KEY"
    assert _classify_fetch_error("dhan: empty series for NSE:X") == "EMPTY_SERIES"
    assert _classify_fetch_error("429 rate limit") == "TRANSIENT"


def test_resolve_aliases_by_isin():
    old = Equity(
        instrument_id=InstrumentId.equity("NSE", "HEG"),
        symbol="HEG",
        exchange=__import__("tradex_domain.enums", fromlist=["ExchangeId"]).ExchangeId.NSE,
        meta=InstrumentMeta(isin="INE545A01024"),
    )
    new = Equity.of("NSE", "HEGAM")
    registry = MagicMock()
    registry.provider_key.side_effect = lambda iid: (
        None if iid == old.instrument_id else "NSE:1336"
    )
    registry.meta.side_effect = lambda iid: (
        {"series": "EQ", "isin": "INE545A01024", "asset_class": "EQUITY"}
        if iid == new.instrument_id else {}
    )

    broker = MagicMock()
    broker.registry = registry
    broker._loaded_instruments = [new]

    result = resolve_universe_symbols(broker, [old])
    assert result.quarantine == []
    assert result.renamed == [("HEG", "HEGAM")]
    registry.register_authoritative.assert_called_once()
    assert result.ok == [old]


def test_simple_sync_no_provider_key_is_failed():
    from unittest.mock import patch

    err = ["NSE:HEG: no provider key for NSE:HEG"]
    with patch("tradex_trading.datalake.simple_sync.ParallelHistoryFetcher") as phf:
        fetcher = MagicMock()
        fetcher.fetch.return_value = ({}, err)
        phf.return_value = fetcher
        store = MagicMock()
        result = simple_sync(
            MagicMock(), store, [Equity.of("NSE", "HEG")], "1m",
            datetime(2026, 9, 1), datetime(2026, 9, 2),
        )
    assert result.failed == ["HEG"]
    assert result.skipped == []
