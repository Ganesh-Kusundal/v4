from __future__ import annotations

import asyncio

from gateway_support import FakeBroker, equity, gateway_config, make_depth, make_quote
from tradex_gateway.brokers import BrokerRuntime
from tradex_gateway.directory import InstrumentDirectory
from tradex_gateway.live import LiveHub
from tradex_gateway.router import MarketDataRouter


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
        removed = hub.unsubscribe(
            "c2", subscription_id=second_ack["subscription_id"]
        )
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
