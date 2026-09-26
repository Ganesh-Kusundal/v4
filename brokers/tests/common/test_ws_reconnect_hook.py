"""Wave B0: AutoReconnectMixin.set_reconnect_hook wiring tests.

Verifies that:
1. set_reconnect_hook() registers a callback on any AutoReconnectMixin backend.
2. The hook fires exactly once after each successful socket reconnect (the
   _reconnect_worker path), NOT on the initial _ensure_ws connection.
3. Hook exceptions are swallowed and do not abort the receive loop.
4. The hook can be cleared (set to None) without crashing.
5. Multiple reconnects each fire the hook once.

These tests use the same deterministic FakeWS harness as the reconnect soak
suite — no network, no real sockets.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from tradex_domain.instruments import Equity, Instrument
from tradex_domain.wire import InstrumentRegistry
from tradex_domain.value_objects import InstrumentId

from tradex_brokers.common.ws_reconnect import WSReconnectManager


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _dhan_registry() -> InstrumentRegistry:
    reg = InstrumentRegistry()
    iid = InstrumentId.equity("NSE", "RELIANCE")
    reg.register(iid, {"key": "2885", "asset_class": "EQUITY"})
    reg.add_alias("2885", iid)
    reg.add_alias("RELIANCE", iid)
    return reg


def _equity() -> Instrument:
    return Equity.of("NSE", "RELIANCE")


def _fast_reconnect(backend: Any) -> None:
    """Override reconnect manager with tiny delays for deterministic tests."""
    backend._reconnect = WSReconnectManager(
        max_retries=200, base_delay=0.005, max_delay=0.01, jitter=False
    )


def _make_harness(make_backend):
    """Return (backend, opened, drop) with a single-drop fake WS.

    The first socket blocks on *drop* then fails; subsequent sockets are
    healthy and block forever (so the receive loop stays alive).
    """
    opened: list[Any] = []
    drop = threading.Event()

    class FakeWS:
        def __init__(self, index: int) -> None:
            self.index = index
            self.sent: list[Any] = []

        def send(self, data: Any) -> None:
            self.sent.append(data)

        def recv(self) -> bytes:
            drop.wait(timeout=5)
            if self.index == 0:
                raise ConnectionError("socket dropped")
            threading.Event().wait()
            raise ConnectionError("socket closed by test")

        def close(self) -> None:
            pass

    def factory(url: str) -> FakeWS:
        ws = FakeWS(index=len(opened))
        opened.append(ws)
        return ws

    backend = make_backend(factory)
    _fast_reconnect(backend)
    return backend, opened, drop


def _dhan_market(factory):
    from tradex_brokers.dhan.ws_streams import DhanMarketDataStreamBackend

    return DhanMarketDataStreamBackend(
        token_provider=lambda: "tok",
        client_id="TEST",
        registry=_dhan_registry(),
        ws_factory=factory,
    )


def _dhan_depth(factory):
    from tradex_brokers.dhan.ws_streams import DhanDepthStreamBackend

    return DhanDepthStreamBackend(
        token_provider=lambda: "tok",
        client_id="TEST",
        registry=_dhan_registry(),
        total_slots=20,
        ws_factory=factory,
    )


def _upstox_market(factory):
    from tradex_brokers.upstox.ws_streams import UpstoxMarketDataStreamBackend

    return UpstoxMarketDataStreamBackend(
        authorize_url="https://example/authorize",
        ws_fetch=lambda *a, **k: (200, {"data": {"authorized_redirect_uri": "wss://x"}}),
        token_provider=lambda: "tok",
        registry=_dhan_registry(),
        ws_factory=factory,
    )


MARKET_BACKENDS = {
    "dhan_market": _dhan_market,
    "dhan_depth": _dhan_depth,
    "upstox_market": _upstox_market,
}

_SUBSCRIBE_METHODS = {
    "dhan_market": "subscribe_quotes",
    "dhan_depth": "subscribe_depth",
    "upstox_market": "subscribe_quotes",
}


def _subscribe(backend: Any, name: str) -> None:
    method = _SUBSCRIBE_METHODS[name]
    getattr(backend, method)([_equity()], lambda _m: None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHookFiresOnReconnect:
    """set_reconnect_hook fires once after each broker socket reconnect."""

    def _assert_hook_fires(self, name: str) -> None:
        backend, opened, drop = _make_harness(MARKET_BACKENDS[name])
        calls: list[int] = []
        backend.set_reconnect_hook(lambda: calls.append(1))
        _subscribe(backend, name)

        assert len(opened) == 1, f"{name}: initial socket not opened"
        assert calls == [], f"{name}: hook must not fire on initial connect"

        drop.set()
        deadline = time.monotonic() + 5
        while len(opened) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(opened) >= 2, f"{name}: backend did not reconnect"

        # Give the reconnect worker a moment to fire the hook
        deadline = time.monotonic() + 2
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls == [1], f"{name}: hook fired {len(calls)} times instead of 1"
        backend.close()

    def test_dhan_market_hook_fires(self) -> None:
        self._assert_hook_fires("dhan_market")

    def test_dhan_depth_hook_fires(self) -> None:
        self._assert_hook_fires("dhan_depth")

    def test_upstox_market_hook_fires(self) -> None:
        self._assert_hook_fires("upstox_market")


class TestHookNotFiredOnInitialConnect:
    """The hook must NOT fire on the initial _ensure_ws connection."""

    def test_initial_connect_does_not_fire_hook(self) -> None:
        backend, opened, _drop = _make_harness(_dhan_market)
        calls: list[int] = []
        backend.set_reconnect_hook(lambda: calls.append(1))

        # Subscribe → triggers _ensure_ws (initial connect)
        backend.subscribe_quotes([_equity()], lambda _m: None)

        # Give the receive loop a moment to start
        time.sleep(0.05)
        assert calls == [], "hook must not fire on initial _ensure_ws connection"
        backend.close()


class TestHookExceptionDoesNotBreakReconnect:
    """A hook that raises must not prevent the receive loop from restarting."""

    def test_hook_exception_swallowed(self) -> None:
        backend, opened, drop = _make_harness(_dhan_market)
        backend.set_reconnect_hook(lambda: 1 / 0)  # always raises ZeroDivisionError
        backend.subscribe_quotes([_equity()], lambda _m: None)

        drop.set()
        deadline = time.monotonic() + 5
        while len(opened) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(opened) >= 2, "backend did not reconnect despite raising hook"

        # Verify the backend settled on the new socket (receive loop running)
        deadline = time.monotonic() + 2
        while backend._ws is not opened[-1] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert backend._ws is opened[-1], "receive loop did not start after hook exception"
        backend.close()


class TestHookCanBeCleared:
    """set_reconnect_hook(None) clears the callback without crashing."""

    def test_none_hook_is_safe(self) -> None:
        backend, opened, drop = _make_harness(_dhan_market)
        backend.set_reconnect_hook(lambda: None)
        backend.set_reconnect_hook(None)  # clear it
        backend.subscribe_quotes([_equity()], lambda _m: None)

        drop.set()
        deadline = time.monotonic() + 5
        while len(opened) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(opened) >= 2, "backend did not reconnect with cleared hook"
        backend.close()


class TestHookFiresOnEachReconnect:
    """Each socket reconnect fires the hook exactly once."""

    DROPS = 3

    def test_multiple_reconnects_each_fire_hook(self) -> None:
        kills: list[threading.Event] = []

        class FakeWS:
            def __init__(self) -> None:
                self.kill = threading.Event()
                self.sent: list[Any] = []
                kills.append(self.kill)

            def send(self, data: Any) -> None:
                self.sent.append(data)

            def recv(self) -> bytes:
                self.kill.wait(timeout=10)
                raise ConnectionError("killed")

            def close(self) -> None:
                pass

        opened: list[FakeWS] = []

        def factory(url: str) -> FakeWS:
            ws = FakeWS()
            opened.append(ws)
            return ws

        backend = _dhan_market(factory)
        backend._reconnect = WSReconnectManager(
            max_retries=1000, base_delay=0.005, max_delay=0.01, jitter=False
        )

        calls: list[int] = []
        backend.set_reconnect_hook(lambda: calls.append(1))
        backend.subscribe_quotes([_equity()], lambda _m: None)
        assert len(opened) == 1
        assert calls == []  # no hook on initial connect

        for i in range(self.DROPS):
            kills[i].set()
            deadline = time.monotonic() + 5
            while len(opened) < i + 2 and time.monotonic() < deadline:
                time.sleep(0.005)
            assert len(opened) >= i + 2, f"drop {i + 1} did not reopen socket"
            # Wait for hook to fire
            hook_deadline = time.monotonic() + 2
            while len(calls) < i + 1 and time.monotonic() < hook_deadline:
                time.sleep(0.005)
            assert len(calls) == i + 1, (
                f"after drop {i + 1}: expected {i + 1} hook calls, got {len(calls)}"
            )

        backend.close()
