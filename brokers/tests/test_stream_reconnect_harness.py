"""Reconnect-heal harness across every broker WebSocket backend.

Deterministic, network-free: each backend gets a ``FakeWS`` whose ``recv``
blocks until the test drops it, so subscriptions land before the failure.
Force-killing the socket must reopen a fresh one and replay the live wire
set (Dhan market/depth resubscribe; order/portfolio feeds push updates).

Covers: Dhan order, Dhan market, Dhan depth, Upstox portfolio, Upstox market.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from tradex_domain.instruments import Equity, Instrument
from tradex_domain.value_objects import InstrumentId
from tradex_domain.wire import InstrumentRegistry

from tradex_brokers.common.ws_reconnect import WSReconnectManager


def _dhan_registry() -> InstrumentRegistry:
    reg = InstrumentRegistry()
    iid = InstrumentId.equity("NSE", "RELIANCE")
    reg.register(iid, {"key": "2885", "asset_class": "EQUITY"})
    reg.add_alias("2885", iid)
    reg.add_alias("RELIANCE", iid)
    return reg


def _upstox_registry() -> InstrumentRegistry:
    reg = InstrumentRegistry()
    iid = InstrumentId.equity("NSE", "RELIANCE")
    reg.register(iid, {"key": "NSE_EQ|INE002A01018", "asset_class": "EQUITY"})
    reg.add_alias("NSE_EQ|INE002A01018", iid)
    reg.add_alias("RELIANCE", iid)
    return reg


def _equity() -> Instrument:
    return Equity.of("NSE", "RELIANCE")


def _map_order(row: dict) -> object:
    from tradex_domain.execution import Order

    return Order.from_dict(row)


def _make_harness(make_backend):
    """Return (backend, opened_sockets, drop_event) with tiny reconnect backoff.

    Socket semantics: the FIRST socket blocks until the test sets *drop*,
    then fails (the forced outage). Any socket reopened afterwards is healthy
    and keeps streaming, so the backend settles on it instead of churning
    through reconnects.
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
            # Healthy socket — keep the receive loop alive until teardown.
            threading.Event().wait()
            raise ConnectionError("socket closed by test")

        def close(self) -> None:
            pass

    def factory(url: str) -> FakeWS:
        ws = FakeWS(index=len(opened))
        opened.append(ws)
        return ws

    backend = make_backend(factory)
    backend._reconnect = WSReconnectManager(
        max_retries=200, base_delay=0.01, max_delay=0.02, jitter=False
    )
    return backend, opened, drop


# ---------------------------------------------------------------------------
# Backend builders (one per stream type)
# ---------------------------------------------------------------------------


def _dhan_order(factory):
    from tradex_brokers.dhan.ws_streams import DhanOrderStreamBackend

    return DhanOrderStreamBackend(
        token_provider=lambda: "tok",
        client_id="TEST123",
        map_order=_map_order,
        ws_factory=factory,
    )


def _dhan_market(factory):
    from tradex_brokers.dhan.ws_streams import DhanMarketDataStreamBackend

    return DhanMarketDataStreamBackend(
        token_provider=lambda: "tok",
        client_id="TEST123",
        registry=_dhan_registry(),
        ws_factory=factory,
    )


def _dhan_depth(factory):
    from tradex_brokers.dhan.ws_streams import DhanDepthStreamBackend

    return DhanDepthStreamBackend(
        token_provider=lambda: "tok",
        client_id="TEST123",
        registry=_dhan_registry(),
        total_slots=20,
        ws_factory=factory,
    )


def _upstox_portfolio(factory):
    from tradex_brokers.upstox.ws_streams import UpstoxPortfolioStreamBackend

    return UpstoxPortfolioStreamBackend(
        authorize_url="https://example/portfolio/authorize",
        ws_fetch=lambda *a, **k: (200, {"data": {"authorized_redirect_uri": "wss://x"}}),
        token_provider=lambda: "tok",
        map_order=_map_order,
        ws_factory=factory,
    )


def _upstox_market(factory):
    from tradex_brokers.upstox.ws_streams import UpstoxMarketDataStreamBackend

    return UpstoxMarketDataStreamBackend(
        authorize_url="https://example/authorize",
        ws_fetch=lambda *a, **k: (200, {"data": {"authorized_redirect_uri": "wss://x"}}),
        token_provider=lambda: "tok",
        registry=_upstox_registry(),
        ws_factory=factory,
    )


BACKENDS = {
    "dhan_order": _dhan_order,
    "dhan_market": _dhan_market,
    "dhan_depth": _dhan_depth,
    "upstox_portfolio": _upstox_portfolio,
    "upstox_market": _upstox_market,
}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _subscribe_all(backend: Any) -> None:
    """Subscribe a representative payload on *backend* (best-effort)."""
    from tradex_domain.errors import CapabilityNotSupportedError

    inst = _equity()
    for name in ("subscribe_quotes", "subscribe_depth_30", "subscribe_depth"):
        fn = getattr(backend, name, None)
        if callable(fn):
            try:
                fn([inst], lambda _m: None)
            except CapabilityNotSupportedError:
                continue  # backend doesn't carry market data; try the next
            return
    for name in ("subscribe_orders",):
        fn = getattr(backend, name, None)
        if callable(fn):
            fn(lambda _m: None)
            return


def _assert_reopened(backend: Any, opened: list[Any], drop: threading.Event) -> None:
    """Kill the live socket; assert the backend reopens a fresh one."""
    drop.set()
    deadline = time.monotonic() + 5
    while len(opened) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(opened) >= 2, "reconnect did not open a fresh socket"
    # The new socket must be the backend's current live socket.
    assert backend._ws is opened[-1]
    backend.close()


class TestReconnectSoak:
    """100 sequential forced drops per backend — reopen rate must be 100%.

    Each socket is killed on demand via its own event (the global-drop design
    above only fails the first socket once). Reopened sockets stay healthy
    until the next kill, so every iteration settles deterministically.
    """

    ITERATIONS = 100

    def _soak(self, name: str, subscribe) -> None:
        opened: list[Any] = []
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
                raise ConnectionError("socket killed")

            def close(self) -> None:
                pass

        def factory(url: str) -> FakeWS:
            ws = FakeWS()
            opened.append(ws)
            return ws

        backend = BACKENDS[name](factory)
        backend._reconnect = WSReconnectManager(
            max_retries=1000, base_delay=0.005, max_delay=0.01, jitter=False
        )
        subscribe(backend)
        assert len(opened) == 1
        for i in range(self.ITERATIONS):
            kills[i].set()  # kill the current socket
            deadline = time.monotonic() + 5
            while len(opened) < i + 2 and time.monotonic() < deadline:
                time.sleep(0.005)
            assert len(opened) >= i + 2, (
                f"{name}: drop {i + 1} did not reopen a socket "
                f"(opened={len(opened)})"
            )
            # Poll for the backend to settle on the fresh socket: the factory
            # appends to *opened* before ``_ensure_ws`` assigns ``self._ws``,
            # so ``len(opened)`` alone can race the reconnect worker.
            settle_deadline = time.monotonic() + 5
            while time.monotonic() < settle_deadline:
                if backend._ws is opened[-1]:
                    break
                time.sleep(0.005)
            assert backend._ws is opened[-1], (
                f"{name}: drop {i + 1} did not settle on the fresh socket"
            )
        backend.close()
        # Every socket except the final one was killed; no further reopens.
        # ``close()`` sets ``_closing`` so a straggler reconnect worker cannot
        # open a stray socket afterwards (it re-checks under the connect lock).
        time.sleep(0.05)
        assert len(opened) == self.ITERATIONS + 1

    def test_soak_dhan_order(self):
        self._soak("dhan_order", lambda b: b.subscribe_orders(lambda _m: None))

    def test_soak_dhan_market(self):
        self._soak(
            "dhan_market", lambda b: b.subscribe_quotes([_equity()], lambda _m: None)
        )

    def test_soak_dhan_depth(self):
        self._soak(
            "dhan_depth", lambda b: b.subscribe_depth([_equity()], lambda _m: None)
        )

    def test_soak_upstox_portfolio(self):
        self._soak(
            "upstox_portfolio", lambda b: b.subscribe_orders(lambda _m: None)
        )

    def test_soak_upstox_market(self):
        self._soak(
            "upstox_market", lambda b: b.subscribe_quotes([_equity()], lambda _m: None)
        )


class TestReconnectHealHarness:
    """Every backend re-heals after a socket drop (deterministic fake WS)."""

    def test_all_backends_reopen_after_drop(self):
        for name, builder in BACKENDS.items():
            backend, opened, drop = _make_harness(builder)
            _subscribe_all(backend)
            assert len(opened) == 1, f"{name}: initial socket not opened"
            _assert_reopened(backend, opened, drop)

    def test_market_backend_replays_wire_set_on_reopen(self):
        """Dhan/Upstox market feeds must re-send the subscribed instruments."""
        for name in ("dhan_market", "upstox_market"):
            backend, opened, drop = _make_harness(BACKENDS[name])
            inst = _equity()
            backend.subscribe_quotes([inst], lambda _m: None)
            drop.set()
            deadline = time.monotonic() + 5
            while len(opened) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(opened) >= 2
            frames = [
                raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
                for raw in opened[1].sent
            ]
            joined = " ".join(frames)
            if name == "dhan_market":
                assert "2885" in joined, "Dhan market did not replay security id"
            else:
                assert "INE002A01018" in joined, "Upstox market did not replay instrument key"
            backend.close()

    def test_depth_backend_replays_wire_set_on_reopen(self):
        """Dhan depth-20 must re-send its subscribed security on reopen."""
        backend, opened, drop = _make_harness(BACKENDS["dhan_depth"])
        inst = _equity()
        backend.subscribe_depth([inst], lambda _m: None)
        drop.set()
        deadline = time.monotonic() + 5
        while len(opened) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(opened) >= 2
        frames = [
            raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
            for raw in opened[1].sent
        ]
        assert "2885" in " ".join(frames), "Dhan depth did not replay security id"
        backend.close()

    def test_order_feeds_reopen_without_resubscribe(self):
        """Order/portfolio feeds push updates — reopen must not crash without frames."""
        for name in ("dhan_order", "upstox_portfolio"):
            backend, opened, drop = _make_harness(BACKENDS[name])
            backend.subscribe_orders(lambda _m: None)
            drop.set()
            deadline = time.monotonic() + 5
            while len(opened) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(opened) >= 2
            assert backend._ws is opened[-1]
            backend.close()

    def test_close_prevents_reconnect(self):
        """After close(), a drop must NOT reopen a socket (clean teardown)."""
        for name, builder in BACKENDS.items():
            backend, opened, drop = _make_harness(builder)
            _subscribe_all(backend)
            backend.close()  # closing flag set; no reconnect allowed
            drop.set()
            time.sleep(0.1)  # give any stray reconnect worker a chance
            assert len(opened) == 1, f"{name}: closed backend reopened a socket"
