from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta

import pytest
from gateway_support import FakeBroker, gateway_config, make_depth
from tradex_domain.errors import CapabilityNotSupportedError, RateLimitError
from tradex_domain.instruments import Future, Option
from tradex_gateway.brokers import BrokerRuntime
from tradex_gateway.directory import InstrumentDirectory
from tradex_gateway.errors import NoHealthyBroker
from tradex_gateway.history import to_epoch_ms
from tradex_gateway.market import CachedDepth, ChainService, DepthService, QuoteService
from tradex_gateway.router import MarketDataRouter


def _setup(*brokers: FakeBroker):
    runtimes = {broker.name: BrokerRuntime(broker.name, broker) for broker in brokers}
    config = gateway_config()
    directory = InstrumentDirectory(runtimes)
    router = MarketDataRouter(config, runtimes)
    return directory, router


def _batch_recording_then_failing(broker: FakeBroker, error: Exception):
    """A ``quote_batch`` stand-in that records the attempt, then raises."""
    record = broker.quote_batch

    def fail(instruments):
        record(instruments)
        raise error

    return fail


def _quote_recording_then_failing(broker: FakeBroker, error: Exception):
    """A ``get_quote`` stand-in that records the attempt, then raises."""
    record = broker.get_quote

    def fail(instrument):
        record(instrument)
        raise error

    return fail


def test_quote_and_batch_shapes():
    directory, router = _setup(FakeBroker("dhan"), FakeBroker("upstox"))
    quotes = QuoteService(directory, router)
    single = quotes.quote("IDX:NIFTY")
    assert single["instrument"] == "IDX:NIFTY"
    assert single["broker"] == "upstox"
    batch = quotes.quotes(["IDX:NIFTY"])
    assert "IDX:NIFTY" in batch["quotes"]
    assert batch["errors"] == {}


def test_quote_batch_outage_does_not_fan_out():
    dhan, upstox = FakeBroker("dhan"), FakeBroker("upstox")
    dhan.quote_batch = _batch_recording_then_failing(dhan, RateLimitError("429"))
    upstox.quote_batch = _batch_recording_then_failing(upstox, RateLimitError("429"))
    directory, router = _setup(dhan, upstox)
    quotes = QuoteService(directory, router)
    with pytest.raises(NoHealthyBroker):
        quotes.quotes(["IDX:NIFTY"])
    for broker in (dhan, upstox):
        # Both brokers were attempted and neither fell back to singles.
        assert any(call[0] == "quote_batch" for call in broker.calls)
        assert all(call[0] != "get_quote" for call in broker.calls)


def test_quote_batch_capability_gap_still_fans_out():
    dhan, upstox = FakeBroker("dhan"), FakeBroker("upstox")
    for broker in (dhan, upstox):
        broker.quote_batch = _batch_recording_then_failing(
            broker, CapabilityNotSupportedError("batch unsupported")
        )
    # The quotes primary cannot answer singles either, so the per-instrument
    # fallback has to keep going and touch both adapters.
    upstox.get_quote = _quote_recording_then_failing(
        upstox, CapabilityNotSupportedError("quotes unsupported")
    )
    directory, router = _setup(dhan, upstox)
    quotes = QuoteService(directory, router)
    result = quotes.quotes(["IDX:NIFTY"])
    assert isinstance(result, dict)
    assert "IDX:NIFTY" in result["quotes"]
    for broker in (dhan, upstox):
        assert any(call[0] == "get_quote" for call in broker.calls)


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
    cached = CachedDepth(
        depth=make_depth(directory.resolve("IDX:NIFTY").instrument, levels=20),
        broker="dhan",
        received_at=datetime.now(UTC),
    )
    depth = DepthService(directory, router, live_depth=lambda _: cached)
    result = depth.depth("IDX:NIFTY", 10)
    assert result["source"] == "ws_cache"
    assert result["delivered_levels"] == 10
    assert result["degraded"] is False


def test_depth_freshness_uses_receipt_time_not_exchange_timestamp():
    directory, router = _setup(FakeBroker("dhan"), FakeBroker("upstox"))
    illiquid = dataclasses.replace(
        make_depth(directory.resolve("IDX:NIFTY").instrument, levels=20),
        timestamp=datetime.now(UTC) - timedelta(hours=1),
    )
    cached = CachedDepth(depth=illiquid, broker="dhan", received_at=datetime.now(UTC))
    service = DepthService(directory, router, live_depth=lambda _: cached)
    result = service.depth("IDX:NIFTY", 10)
    assert result["source"] == "ws_cache"
    assert result["ts"] == to_epoch_ms(illiquid.timestamp)


def test_depth_cache_older_than_ttl_falls_back_to_rest():
    directory, router = _setup(FakeBroker("dhan"), FakeBroker("upstox"))
    cached = CachedDepth(
        depth=make_depth(directory.resolve("IDX:NIFTY").instrument, levels=20),
        broker="dhan",
        received_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    service = DepthService(
        directory,
        router,
        live_depth=lambda _: cached,
        live_depth_ttl=5.0,
    )
    result = service.depth("IDX:NIFTY", 10)
    assert result["source"] == "rest"
    assert result["degraded"] is True


def test_depth_cache_with_naive_receipt_time_falls_back_to_rest():
    directory, router = _setup(FakeBroker("dhan"), FakeBroker("upstox"))
    cached = CachedDepth(
        depth=make_depth(directory.resolve("IDX:NIFTY").instrument, levels=20),
        broker="dhan",
        received_at=datetime(2026, 11, 6, 9, 15),
    )
    service = DepthService(
        directory,
        router,
        live_depth=lambda _: cached,
        live_depth_ttl=5.0,
    )
    result = service.depth("IDX:NIFTY", 10)
    assert result["source"] == "rest"
    assert result["degraded"] is True


def test_depth_cache_with_future_receipt_time_falls_back_to_rest():
    directory, router = _setup(FakeBroker("dhan"), FakeBroker("upstox"))
    cached = CachedDepth(
        depth=make_depth(directory.resolve("IDX:NIFTY").instrument, levels=20),
        broker="dhan",
        received_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    service = DepthService(
        directory,
        router,
        live_depth=lambda _: cached,
        live_depth_ttl=5.0,
    )
    result = service.depth("IDX:NIFTY", 10)
    assert result["source"] == "rest"
    assert result["degraded"] is True


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


def test_chain_enrichment_prefers_the_chain_broker():
    dhan, upstox = FakeBroker("dhan"), FakeBroker("upstox")
    expiry = date.today() + timedelta(days=7)
    dhan.universe = [
        Option.of("NFO", "NIFTY", expiry, 25000, "CE"),
        Option.of("NFO", "NIFTY", expiry, 25000, "PE"),
    ]
    directory, router = _setup(dhan, upstox)
    chain_service = ChainService(directory, router, QuoteService(directory, router))
    result = chain_service.option_chain("IDX:NIFTY", live=True)
    assert result["live"] is True
    assert any(call[0] == "quote_batch" for call in dhan.calls)
    assert not any(call[0] == "quote_batch" for call in upstox.calls)


def test_futures_are_master_derived():
    dhan = FakeBroker("dhan")
    expiry = date.today() + timedelta(days=30)
    dhan.futures = [Future.of("NFO", "NIFTY", expiry)]
    directory, router = _setup(dhan, FakeBroker("upstox"))
    chain_service = ChainService(directory, router, QuoteService(directory, router))
    result = chain_service.futures("IDX:NIFTY")
    assert result["futures"][0]["expiry"] == expiry.isoformat()


def test_option_chain_and_futures_include_lot_size():
    expiry = date.today() + timedelta(days=7)
    future = Future.of("NFO", "NIFTY", date.today() + timedelta(days=17))
    ce = Option.of("NFO", "NIFTY", expiry, 25000, "CE")
    pe = Option.of("NFO", "NIFTY", expiry, 25000, "PE")
    dhan = FakeBroker("dhan")
    dhan.futures = [future]
    dhan.registry.lot_sizes = {
        str(ce.instrument_id): 75,
        str(pe.instrument_id): 75,
        str(future.instrument_id): 50,
    }
    directory, router = _setup(dhan, FakeBroker("upstox"))
    chain_service = ChainService(directory, router, QuoteService(directory, router))

    chain = chain_service.option_chain("IDX:NIFTY")
    assert chain["strikes"][0]["ce"]["lot_size"] == 75
    assert chain["strikes"][0]["pe"]["lot_size"] == 75

    futures = chain_service.futures("IDX:NIFTY")
    assert futures["futures"][0]["lot_size"] == 50
