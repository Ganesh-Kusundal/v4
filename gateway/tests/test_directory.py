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
    directory = _directory(
        FakeBroker("dhan", universe=[index(), option(expiry=expiry)])
    )
    assert directory.nearest_expiry(index()) == expiry
