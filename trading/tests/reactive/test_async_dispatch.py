"""Tests for AsyncDispatch."""

from __future__ import annotations

from typing import Any

from tradex_trading.reactive.async_dispatch import AsyncDispatch
from tradex_trading.reactive.bus import ReactiveBus

# ------------------------------------------------------------------ #
# 1. subscribe_async — handler registration
# ------------------------------------------------------------------ #

def test_subscribe_async():
    bus = ReactiveBus()
    dispatch = AsyncDispatch(bus)

    async def handler(msg: Any) -> None:
        pass

    dispatch.subscribe_async(handler)
    assert len(dispatch._handlers) == 1
    assert dispatch._handlers[0] is handler


# ------------------------------------------------------------------ #
# 2. publish_async — message reaches bus (sync fallback)
# ------------------------------------------------------------------ #

def test_publish_async():
    bus = ReactiveBus()
    dispatch = AsyncDispatch(bus)
    received: list[Any] = []

    bus.subscribe(on_next=received.append)

    dispatch.publish_async("hello")
    assert received == ["hello"]


# ------------------------------------------------------------------ #
# 3. dispose — cleanup
# ------------------------------------------------------------------ #

def test_dispose():
    bus = ReactiveBus()
    dispatch = AsyncDispatch(bus)

    async def handler(msg: Any) -> None:
        pass

    dispatch.subscribe_async(handler)
    assert len(dispatch._handlers) == 1

    dispatch.dispose()
    assert dispatch._handlers == []


# ------------------------------------------------------------------ #
# 4. without loop — sync fallback
# ------------------------------------------------------------------ #

def test_without_loop():
    bus = ReactiveBus()
    dispatch = AsyncDispatch(bus, loop=None)

    received: list[Any] = []
    bus.subscribe(on_next=received.append)

    dispatch.publish_async({"event": "test"})
    assert len(received) == 1
    assert received[0] == {"event": "test"}
