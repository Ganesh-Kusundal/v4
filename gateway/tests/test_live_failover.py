from __future__ import annotations

import asyncio

from gateway_support import FakeBroker, gateway_config
from tradex_gateway.brokers import BrokerRuntime
from tradex_gateway.directory import InstrumentDirectory
from tradex_gateway.live import LiveHub
from tradex_gateway.router import MarketDataRouter


def test_hub_halts_without_recursing_when_both_feeds_fail():
    async def scenario() -> None:
        dhan, upstox = FakeBroker("dhan"), FakeBroker("upstox")
        dhan.fail_subscribe = True
        upstox.fail_subscribe = True
        runtimes = {
            "dhan": BrokerRuntime("dhan", dhan),
            "upstox": BrokerRuntime("upstox", upstox),
        }
        directory = InstrumentDirectory(runtimes)
        config = gateway_config()
        router = MarketDataRouter(config, runtimes)
        hub = LiveHub(config, router, directory, runtimes)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        control: asyncio.Queue = asyncio.Queue()
        hub.add_client("c1", loop, queue, control)
        ack = hub.subscribe("c1", ["NSE:RELIANCE"], "quote", 5)
        assert ack["op"] == "ack"
        assert hub.status()["state"] == "halted"
        hub.stop()

    asyncio.run(scenario())
