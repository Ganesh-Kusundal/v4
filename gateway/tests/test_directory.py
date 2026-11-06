from __future__ import annotations

from datetime import date, timedelta

from gateway_support import FakeBroker, equity, index, option
from tradex_domain.instruments import Index
from tradex_gateway.brokers import BrokerRuntime
from tradex_gateway.directory import InstrumentDirectory


def _directory(*brokers: FakeBroker) -> InstrumentDirectory:
    return InstrumentDirectory(
        {broker.name: BrokerRuntime(broker.name, broker) for broker in brokers}
    )


def test_index_id_resolves_to_index_not_equity():
    directory = _directory(FakeBroker("dhan"))
    resolved = directory.resolve("IDX:NIFTY")
    assert isinstance(resolved.instrument, Index)
    assert str(resolved.instrument_id) == "IDX:NIFTY"
    assert resolved.brokers == ("dhan",)


def test_resolve_accepts_asset_class_hint():
    directory = _directory(FakeBroker("dhan"))
    resolved = directory.resolve("NSE:NIFTY", asset_class="index")
    assert isinstance(resolved.instrument, Index)
    assert str(resolved.instrument_id) == "IDX:NIFTY"


def test_resolve_only_reports_brokers_that_know_the_instrument():
    dhan = FakeBroker("dhan", universe=[equity("RELIANCE")])
    upstox = FakeBroker("upstox", universe=[])
    directory = _directory(dhan, upstox)
    resolved = directory.resolve("NSE:RELIANCE")
    assert resolved.brokers == ("dhan",)


def test_search_dedupes_and_lists_brokers():
    workspace = [equity("RELIANCE"), option()]
    directory = _directory(
        FakeBroker("dhan", universe=workspace),
        FakeBroker("upstox", universe=workspace),
    )
    rows = directory.search("REL")
    assert len(rows) == 1
    assert rows[0]["instrument_id"] == "NSE:RELIANCE"
    assert rows[0]["brokers"] == ["dhan", "upstox"]


def test_nearest_expiry_uses_master():
    expiry = date.today() + timedelta(days=10)
    directory = _directory(FakeBroker("dhan", universe=[index(), option(expiry=expiry)]))
    assert directory.nearest_expiry(index()) == expiry


def test_lot_size_accepts_dhan_float_strings():
    directory = _directory(FakeBroker("dhan", universe=[equity("RELIANCE")]))
    directory._runtimes["dhan"].broker.registry.lot_sizes = {"NSE:RELIANCE": "500.0"}
    assert directory.lot_size("NSE:RELIANCE") == 500


def test_search_and_resolve_include_lot_size():
    workspace = [equity("RELIANCE"), option()]
    dhan = FakeBroker("dhan", universe=workspace)
    upstox = FakeBroker("upstox", universe=workspace)
    dhan.registry.lot_sizes = {"NSE:RELIANCE": 250}
    upstox.registry.lot_sizes = {"NSE:RELIANCE": 250}
    directory = _directory(dhan, upstox)

    row = directory.search("REL")[0]
    assert row["lot_size"] == 250
    assert row["lot_size_by_broker"] == {"dhan": 250, "upstox": 250}

    resolved = directory.resolve("NSE:RELIANCE")
    assert resolved.lot_sizes == {"dhan": 250, "upstox": 250}
    meta = InstrumentDirectory.instrument_meta(
        resolved.instrument, resolved.brokers, resolved.lot_sizes
    )
    assert meta["lot_size"] == 250
