from __future__ import annotations

import asyncio
import dataclasses
import threading
import time
from datetime import UTC, datetime

from gateway_support import (
    FakeBroker,
    FakeClock,
    equity,
    gateway_config,
    index,
    make_depth,
    make_quote,
)
from tradex_gateway.brokers import BrokerRuntime
from tradex_gateway.directory import InstrumentDirectory
from tradex_gateway.history import to_epoch_ms
from tradex_gateway.live import LiveHub
from tradex_gateway.market import CachedDepth
from tradex_gateway.router import MarketDataRouter


async def _drain(queue: asyncio.Queue) -> list[dict]:
    await asyncio.sleep(0)
    frames: list[dict] = []
    while not queue.empty():
        frames.append(queue.get_nowait())
    return frames


def test_live_hub_refcounts_fans_out_and_fails_over():
    async def scenario() -> None:
        dhan, upstox = FakeBroker("dhan"), FakeBroker("upstox")
        runtimes = {
            "dhan": BrokerRuntime("dhan", dhan),
            "upstox": BrokerRuntime("upstox", upstox),
        }
        directory = InstrumentDirectory(runtimes)
        router = MarketDataRouter(gateway_config(), runtimes)
        hub = LiveHub(gateway_config(), router, directory, runtimes)
        loop = asyncio.get_running_loop()
        queues: list[asyncio.Queue] = []
        controls: list[asyncio.Queue] = []

        def add_client(client_id: str) -> tuple[asyncio.Queue, asyncio.Queue]:
            queue: asyncio.Queue = asyncio.Queue()
            control: asyncio.Queue = asyncio.Queue()
            hub.add_client(client_id, loop, queue, control)
            queues.append(queue)
            controls.append(control)
            return queue, control

        first, _ = add_client("c1")
        ack = hub.subscribe("c1", ["NSE:RELIANCE"], "quote", 5)
        assert ack["op"] == "ack"
        assert ack["broker"] == "dhan"
        assert dhan.quote_sub_calls == 1

        await asyncio.sleep(0)
        dhan.quote_handlers[0](make_quote(equity()))
        await asyncio.sleep(0)
        frame = first.get_nowait()
        assert frame["op"] == "quote"
        assert frame["instrument"] == "NSE:RELIANCE"

        second, _ = add_client("c2")
        second_ack = hub.subscribe("c2", ["NSE:RELIANCE"], "quote", 5)
        assert dhan.quote_sub_calls == 1
        await asyncio.sleep(0)
        dhan.quote_handlers[0](make_quote(equity(), ltp=101.0))
        await asyncio.sleep(0)
        assert second.get_nowait()["op"] == "quote"
        removed = hub.unsubscribe("c2", subscription_id=second_ack["subscription_id"])
        assert removed["unsubscribed"] == ["NSE:RELIANCE"]
        assert dhan.quote_sub_calls == 1

        depth_queue, _ = add_client("c3")
        hub.subscribe("c3", ["NSE:RELIANCE"], "depth", 10)
        assert dhan.depth_sub_calls == 1
        await asyncio.sleep(0)
        dhan.depth_handlers[0](make_depth(equity(), levels=20))
        await asyncio.sleep(0)
        depth_frame = depth_queue.get_nowait()
        assert depth_frame["op"] == "depth"
        assert len(depth_frame["bids"]) == 10
        assert depth_frame["levels"] == 10

        hub._switch_to("upstox", reason="test")
        assert hub.status()["broker"] == "upstox"
        assert upstox.quote_sub_calls == 1
        assert upstox.depth_sub_calls == 1
        hub.stop()

    asyncio.run(scenario())


def test_switch_does_not_block_quote_publish():
    async def scenario() -> None:
        class BlockingBroker(FakeBroker):
            def __init__(self, name: str) -> None:
                super().__init__(name)
                self.entered = threading.Event()
                self.release = threading.Event()
                self.block = False

            def subscribe_quotes(self, instruments, handler):
                if self.block:
                    self.entered.set()
                    self.release.wait(2.0)
                return super().subscribe_quotes(instruments, handler)

        dhan, upstox = BlockingBroker("dhan"), FakeBroker("upstox")
        runtimes = {
            "dhan": BrokerRuntime("dhan", dhan),
            "upstox": BrokerRuntime("upstox", upstox),
        }
        config = gateway_config()
        directory = InstrumentDirectory(runtimes)
        hub = LiveHub(config, MarketDataRouter(config, runtimes), directory, runtimes)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        control: asyncio.Queue = asyncio.Queue()
        hub.add_client("c1", loop, queue, control)
        hub.subscribe("c1", ["NSE:RELIANCE"], "quote", 5)

        dhan.block = True
        switch = threading.Thread(target=lambda: hub._switch_to("dhan", reason="test"), daemon=True)
        switch.start()
        assert dhan.entered.wait(1.0)

        started = time.monotonic()
        hub._publish_quote(make_quote(equity()), "dhan")
        assert time.monotonic() - started < 0.5

        dhan.release.set()
        switch.join(2.0)
        assert hub.status()["broker"] == "dhan"
        hub.stop()

    asyncio.run(scenario())


def test_staleness_is_reported_per_instrument():
    config = gateway_config(stale_after_seconds=15.0)
    dhan = FakeBroker("dhan")
    runtimes = {"dhan": BrokerRuntime("dhan", dhan)}
    hub = LiveHub(
        config,
        MarketDataRouter(config, runtimes),
        InstrumentDirectory(runtimes),
        runtimes,
    )
    hub._last_tick["IDX:NIFTY"] = 100.0
    hub._last_tick["NSE:RELIANCE"] = 50.0
    assert hub._stale_instruments(110.0, {"IDX:NIFTY", "NSE:RELIANCE"}) == ["NSE:RELIANCE"]
    assert hub._stale_instruments(120.0, {"IDX:NIFTY", "NSE:RELIANCE"}) == [
        "IDX:NIFTY",
        "NSE:RELIANCE",
    ]


def test_monitor_reports_partial_staleness_without_switching(monkeypatch):
    async def scenario() -> None:
        clock = FakeClock(1000.0)
        dhan, upstox = FakeBroker("dhan"), FakeBroker("upstox")
        runtimes = {
            "dhan": BrokerRuntime("dhan", dhan),
            "upstox": BrokerRuntime("upstox", upstox),
        }
        config = gateway_config(stale_after_seconds=15.0)
        hub = LiveHub(
            config,
            MarketDataRouter(config, runtimes),
            InstrumentDirectory(runtimes),
            runtimes,
            clock=clock,
        )
        monkeypatch.setattr(hub, "_ensure_monitor", lambda: None)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        control: asyncio.Queue = asyncio.Queue()
        hub.add_client("c1", loop, queue, control)
        hub.subscribe("c1", ["NSE:RELIANCE", "IDX:NIFTY"], "quote", 5)
        await _drain(control)

        hub._publish_quote(make_quote(equity()), "dhan")
        hub._publish_quote(make_quote(index()), "dhan")
        clock.advance(10.0)
        hub._monitor_cycle()
        assert await _drain(control) == []

        clock.advance(10.0)
        hub._publish_quote(make_quote(equity()), "dhan")
        hub._monitor_cycle()
        assert await _drain(control) == [
            {
                "op": "status",
                "state": "degraded",
                "stale": ["IDX:NIFTY"],
                "broker": "dhan",
            }
        ]
        assert hub.status()["broker"] == "dhan"

        hub._monitor_cycle()
        assert await _drain(control) == []
        hub.stop()

    asyncio.run(scenario())


def test_monitor_switches_only_when_every_instrument_is_stale(monkeypatch):
    async def scenario() -> None:
        clock = FakeClock(1000.0)
        dhan, upstox = FakeBroker("dhan"), FakeBroker("upstox")
        runtimes = {
            "dhan": BrokerRuntime("dhan", dhan),
            "upstox": BrokerRuntime("upstox", upstox),
        }
        config = gateway_config(stale_after_seconds=15.0)
        hub = LiveHub(
            config,
            MarketDataRouter(config, runtimes),
            InstrumentDirectory(runtimes),
            runtimes,
            clock=clock,
        )
        monkeypatch.setattr(hub, "_ensure_monitor", lambda: None)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        control: asyncio.Queue = asyncio.Queue()
        hub.add_client("c1", loop, queue, control)
        hub.subscribe("c1", ["NSE:RELIANCE"], "quote", 5)
        await _drain(control)

        hub._publish_quote(make_quote(equity()), "dhan")
        clock.advance(20.0)
        hub._monitor_cycle()

        assert hub.status()["broker"] == "upstox"
        assert upstox.quote_sub_calls == 1
        health = hub._runtimes["dhan"].health.state("live")
        assert health.consecutive_failures == 1
        assert health.last_error == "stale feed"
        assert hub._next_switch_at == clock.now + 10.0
        frames = await _drain(control)
        assert {
            "op": "status",
            "state": "degraded",
            "stale": ["NSE:RELIANCE"],
            "broker": "dhan",
        } in frames
        assert any(frame["op"] == "gap" and frame["reason"] == "stale_feed" for frame in frames)
        hub.stop()

    asyncio.run(scenario())


def test_monitor_loop_survives_a_failing_cycle():
    class StubStop:
        def __init__(self) -> None:
            self.waits = 0

        def wait(self, timeout: float) -> bool:
            self.waits += 1
            return self.waits > 2

        def set(self) -> None:
            pass

    dhan = FakeBroker("dhan")
    runtimes = {"dhan": BrokerRuntime("dhan", dhan)}
    config = gateway_config()
    hub = LiveHub(
        config, MarketDataRouter(config, runtimes), InstrumentDirectory(runtimes), runtimes
    )
    hub._stop = StubStop()
    calls: list[int] = []

    def flaky_cycle() -> None:
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("boom")

    hub._monitor_cycle = flaky_cycle
    hub._monitor_loop()
    assert len(calls) == 2


def test_latest_depth_carries_receipt_time_and_keeps_exchange_ts(monkeypatch):
    async def scenario() -> None:
        clock = FakeClock(1000.0)
        dhan, upstox = FakeBroker("dhan"), FakeBroker("upstox")
        runtimes = {
            "dhan": BrokerRuntime("dhan", dhan),
            "upstox": BrokerRuntime("upstox", upstox),
        }
        config = gateway_config()
        hub = LiveHub(
            config,
            MarketDataRouter(config, runtimes),
            InstrumentDirectory(runtimes),
            runtimes,
            clock=clock,
        )
        monkeypatch.setattr(hub, "_ensure_monitor", lambda: None)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        control: asyncio.Queue = asyncio.Queue()
        hub.add_client("c1", loop, queue, control)
        hub.subscribe("c1", ["NSE:RELIANCE"], "depth", 10)

        exchange_time = datetime(2026, 11, 6, 9, 15, tzinfo=UTC)
        depth = dataclasses.replace(make_depth(equity(), levels=10), timestamp=exchange_time)
        hub._publish_depth(depth, "dhan")

        cached = hub.latest_depth("NSE:RELIANCE")
        assert isinstance(cached, CachedDepth)
        assert cached.depth is depth
        assert cached.broker == "dhan"
        assert cached.received_at.tzinfo is not None
        assert 0 <= (datetime.now(UTC) - cached.received_at).total_seconds() < 5

        frames = await _drain(queue)
        assert frames[0]["op"] == "depth"
        assert frames[0]["ts"] == to_epoch_ms(exchange_time)
        hub.stop()

    asyncio.run(scenario())
