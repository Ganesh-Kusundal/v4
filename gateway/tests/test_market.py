from __future__ import annotations

from datetime import date, timedelta

from gateway_support import FakeBroker, gateway_config, make_depth
from tradex_domain.instruments import Future, Option
from tradex_gateway.brokers import BrokerRuntime
from tradex_gateway.directory import InstrumentDirectory
from tradex_gateway.market import ChainService, DepthService, QuoteService
from tradex_gateway.router import MarketDataRouter


def _setup(*brokers: FakeBroker):
    runtimes = {broker.name: BrokerRuntime(broker.name, broker) for broker in brokers}
    config = gateway_config()
    directory = InstrumentDirectory(runtimes)
    router = MarketDataRouter(config, runtimes)
    return directory, router


def test_quote_and_batch_shapes():
    directory, router = _setup(FakeBroker("dhan"), FakeBroker("upstox"))
    quotes = QuoteService(directory, router)
    single = quotes.quote("IDX:NIFTY")
    assert single["instrument"] == "IDX:NIFTY"
    assert single["broker"] == "upstox"
    batch = quotes.quotes(["IDX:NIFTY"])
    assert "IDX:NIFTY" in batch["quotes"]
    assert batch["errors"] == {}


def test_depth_degrades_when_rest_cannot_deliver():
    directory, router = _setup(FakeBroker("dhan"), FakeBroker("upstox"))
    depth = DepthService(directory, router)
    shallow = depth.depth("IDX:NIFTY", 5)
    assert shallow["delivered_levels"] == 5
    assert shallow["degraded"] is False
    deep = depth.depth("IDX:NIFTY", 10)
    assert deep["delivered_levels"] == 5
    assert deep["degraded"] is True


def test_depth_uses_live_cache_for_deep_books():
    directory, router = _setup(FakeBroker("dhan"), FakeBroker("upstox"))
    cached = make_depth(
        directory.resolve("IDX:NIFTY").instrument, levels=20
    )
    depth = DepthService(
        directory, router, live_depth=lambda _: (cached, "dhan")
    )
    result = depth.depth("IDX:NIFTY", 10)
    assert result["source"] == "ws_cache"
    assert result["delivered_levels"] == 10
    assert result["degraded"] is False


def test_option_chain_defaults_to_nearest_expiry_and_caches():
    dhan = FakeBroker("dhan")
    expiry = date.today() + timedelta(days=7)
    dhan.universe = [
        Option.of("NFO", "NIFTY", expiry, 25000, "CE"),
        Option.of("NFO", "NIFTY", expiry, 25000, "PE"),
    ]
    directory, router = _setup(dhan, FakeBroker("upstox"))
    chain_service = ChainService(directory, router, QuoteService(directory, router))
    first = chain_service.option_chain("IDX:NIFTY")
    second = chain_service.option_chain("IDX:NIFTY")
    assert first["expiry"] == expiry.isoformat()
    assert first["strikes"][0]["ce"]["instrument_id"].endswith(":CE")
    assert second["cached"] is True
    assert sum(1 for call in dhan.calls if call[0] == "option_chain") == 1


def test_futures_are_master_derived():
    dhan = FakeBroker("dhan")
    expiry = date.today() + timedelta(days=30)
    dhan.futures = [Future.of("NFO", "NIFTY", expiry)]
    directory, router = _setup(dhan, FakeBroker("upstox"))
    chain_service = ChainService(directory, router, QuoteService(directory, router))
    result = chain_service.futures("IDX:NIFTY")
    assert result["futures"][0]["expiry"] == expiry.isoformat()
