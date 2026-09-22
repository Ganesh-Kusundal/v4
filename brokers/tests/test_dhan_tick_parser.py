"""Tests for the Dhan binary market-feed tick parser (RequestCode 15)."""

from __future__ import annotations

import json
import struct
import time
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

from tradex_domain.instruments import Equity
from tradex_domain.value_objects import InstrumentId
from tradex_domain.wire import InstrumentRegistry

from tradex_brokers.common.ws_reconnect import WSReconnectManager
from tradex_brokers.dhan.adapter import DhanBroker
from tradex_brokers.dhan.tick_parser import SEGMENT_EXCHANGE, parse_tick_frame
from tradex_brokers.dhan.ws_streams import DhanMarketDataStreamBackend

#: Real frames captured from wss://api-feed.dhan.co for NSE:RELIANCE (id 2885).
CAPTURED_TICKER = b"\x02\x10\x00\x01E\x0b\x00\x00\x9a\x19\xa5D9\x8atj"
CAPTURED_PREV_CLOSE = b"\x06\x10\x00\x01E\x0b\x00\x00\x00\x00\xa0D\x00\x00\x00\x00"

# ---------------------------------------------------------------------------
# Frame builders (official dhanhq wire format)
# ---------------------------------------------------------------------------


def _ticker(ltp: float = 1322.9, segment: int = 1, security_id: int = 2885) -> bytes:
    return struct.pack("<BHBIfI", 2, 0, segment, security_id, ltp, 1_786_000_000)


def _quote(
    *,
    ltp: float = 1322.9,
    volume: int = 12345,
    segment: int = 1,
    security_id: int = 2885,
) -> bytes:
    return struct.pack(
        "<BHBIfHIfIIIffff",
        4, 0, segment, security_id, ltp, 50, 1_786_000_000, 1320.0, volume, 100, 200,
        1320.0, 1319.5, 1325.0, 1318.0,
    )


def _oi(oi: int = 987654, segment: int = 1, security_id: int = 2885) -> bytes:
    return struct.pack("<BHBII", 5, 0, segment, security_id, oi)


def _prev_close(prev_close: float = 1280.0, segment: int = 1, security_id: int = 2885) -> bytes:
    return struct.pack("<BHBIfI", 6, 0, segment, security_id, prev_close, 42)


def _full(*, ltp: float = 1322.9, segment: int = 1, security_id: int = 2885) -> bytes:
    depth_blob = b"".join(
        struct.pack("<IIHHff", 100 + i, 200 + i, 1, 1, 1320.0 + i, 1322.0 + i) for i in range(5)
    )
    assert len(depth_blob) == 100
    return struct.pack(
        "<BHBIfHIfIIIIIIffff100s",
        8, 0, segment, security_id, ltp, 50, 1_786_000_000, 1320.0, 12345, 100, 200,
        987654, 1300, 1400, 1320.0, 1319.5, 1325.0, 1318.0, depth_blob,
    )


def _disconnect(code: int = 805) -> bytes:
    return struct.pack("<BHBIH", 50, 0, 1, 0, code)


def _no_socket(url: str):
    """Block any real WebSocket connection from tests."""
    raise AssertionError(f"tests must not open real sockets: {url}")


# ---------------------------------------------------------------------------
# Parser unit tests
# ---------------------------------------------------------------------------


class TestParseTickFrame:
    def test_ticker_captured_frame(self):
        row = parse_tick_frame(CAPTURED_TICKER)
        assert row is not None
        assert row["exchange_segment"] == 1
        assert row["security_id"] == 2885
        assert row["last_price"] == 1320.8  # little-endian float from the live frame
        assert row["timestamp"]  # ISO string present
        assert row["depth"] == {"buy": [], "sell": []}

    def test_prev_close_captured_frame(self):
        row = parse_tick_frame(CAPTURED_PREV_CLOSE)
        assert row is not None
        assert row["security_id"] == 2885
        assert row["prev_close"] == 1280.0
        assert row["oi"] == 0

    def test_quote_frame(self):
        row = parse_tick_frame(_quote())
        assert row is not None
        assert row["last_price"] == 1322.9
        assert row["volume"] == 12345
        assert row["ohlc"] == {"open": 1320.0, "high": 1325.0, "low": 1318.0, "close": 1319.5}

    def test_oi_frame(self):
        row = parse_tick_frame(_oi())
        assert row is not None
        assert row["oi"] == 987654
        assert row["security_id"] == 2885

    def test_full_frame_with_depth(self):
        row = parse_tick_frame(_full())
        assert row is not None
        assert row["oi"] == 987654
        assert row["volume"] == 12345
        assert len(row["depth"]["buy"]) == 5
        assert len(row["depth"]["sell"]) == 5
        assert row["depth"]["buy"][0]["price"] == 1320.0
        assert row["depth"]["buy"][0]["quantity"] == 100

    def test_disconnect_frame(self):
        row = parse_tick_frame(_disconnect(code=807))
        assert row == {"type": "disconnect", "exchange_segment": 1, "error_code": 807}

    def test_unknown_type_returns_none(self):
        assert parse_tick_frame(b"\x99\x00\x00") is None

    def test_truncated_frame_returns_none(self):
        assert parse_tick_frame(CAPTURED_TICKER[:8]) is None

    def test_garbage_returns_none(self):
        assert parse_tick_frame(b"") is None
        assert parse_tick_frame(b"\x02") is None

    def test_segment_exchange_mapping(self):
        assert SEGMENT_EXCHANGE[1] == "NSE"
        assert SEGMENT_EXCHANGE[2] == "NFO"
        assert SEGMENT_EXCHANGE[4] == "BSE"

    def test_ltt_ist_wall_emitted_as_utc(self):
        """Dhan LTT wall numbers are IST; Quote consumers need real UTC."""
        from datetime import UTC, datetime
        from zoneinfo import ZoneInfo

        ist = ZoneInfo("Asia/Kolkata")
        wall = datetime(2026, 9, 22, 19, 35, 10)
        # Wire epoch: same wall components stamped as if they were UTC.
        epoch = int(wall.replace(tzinfo=UTC).timestamp())
        row = parse_tick_frame(
            struct.pack("<BHBIfI", 2, 0, 5, 569900, 8680.0, epoch)
        )
        assert row is not None
        expect = wall.replace(tzinfo=ist).astimezone(UTC).isoformat()
        assert row["last_trade_time"] == expect
        assert row["timestamp"] == expect


# ---------------------------------------------------------------------------
# Backend dispatch tests (feed_raw → Quote)
# ---------------------------------------------------------------------------


class TestBackendDispatch:
    def _backend(self, *, with_bare_alias: bool = False) -> DhanMarketDataStreamBackend:
        registry = InstrumentRegistry()
        iid = InstrumentId.equity("NSE", "RELIANCE")
        # Register the way the live master loader does: ``{exchange}:{id}``.
        registry.register(iid, {"key": "NSE:2885", "asset_class": "EQUITY"})
        registry.add_alias("NSE:2885", iid)
        if with_bare_alias:
            registry.add_alias("2885", iid)
        return DhanMarketDataStreamBackend(
            token_provider=lambda: "tok",
            client_id="client",
            registry=registry,
            ws_factory=_no_socket,
        )

    def _register_handler(self, backend: DhanMarketDataStreamBackend, handler) -> None:
        """Attach a quote handler without opening a socket (tests drive feed_raw)."""
        backend._quote_handlers["test"] = handler

    def test_binary_ticker_dispatch_with_segment_fallback(self):
        backend = self._backend()
        received = []
        self._register_handler(backend, lambda q: received.append(q))
        backend.feed_raw(CAPTURED_TICKER)
        assert len(received) == 1
        quote = received[0]
        assert quote.ltp.value == Decimal("1320.8")
        assert quote.instrument.symbol == "RELIANCE"

    def test_binary_ticker_dispatch_with_bare_alias(self):
        backend = self._backend(with_bare_alias=True)
        received = []
        self._register_handler(backend, lambda q: received.append(q))
        backend.feed_raw(_ticker())
        assert len(received) == 1
        assert received[0].ltp.value == Decimal("1322.9")

    def test_json_path_still_works(self):
        backend = self._backend(with_bare_alias=True)
        received = []
        self._register_handler(backend, lambda q: received.append(q))
        frame = '{"ExchangeSegment": "NSE_EQ", "SecurityId": "2885", "last_price": 1300.5}'
        backend.feed_raw(frame)
        assert len(received) == 1
        assert received[0].ltp.value == Decimal("1300.5")

    def test_order_frames_are_skipped(self):
        backend = self._backend(with_bare_alias=True)
        received = []
        self._register_handler(backend, lambda q: received.append(q))
        backend.feed_raw('{"orderId": "1001", "type": "order"}')
        assert received == []

    def test_disconnect_frame_logs_and_arms_reconnect(self, caplog):
        from tradex_brokers.common.ws_reconnect import WSReconnectManager

        backend = self._backend(with_bare_alias=True)
        # Long first delay so the armed thread is deterministically still
        # pending when we assert (it would otherwise fail against _no_socket).
        backend._reconnect = WSReconnectManager(
            max_retries=10, base_delay=1.0, max_delay=2.0, jitter=False
        )
        received = []
        self._register_handler(backend, lambda q: received.append(q))
        with caplog.at_level("WARNING"):
            backend.feed_raw(_disconnect(code=807))
        assert received == []
        assert any("dhan_market_feed_disconnect" in r.message for r in caplog.records)
        # The socket was left in limbo before; a disconnect frame must arm the
        # reconnect machinery (Dhan throttles by closing the feed this way).
        assert backend._ws is None
        assert backend._reconnect_thread is not None
        backend.close()

    def test_prev_close_and_oi_frames_do_not_emit_quotes(self):
        backend = self._backend(with_bare_alias=True)
        received = []
        self._register_handler(backend, lambda q: received.append(q))
        backend.feed_raw(CAPTURED_PREV_CLOSE)
        backend.feed_raw(_oi())
        assert received == []


# ---------------------------------------------------------------------------
# Reconnect tests (AutoReconnectMixin)
# ---------------------------------------------------------------------------


class TestMarketBackendReconnect:
    """A dropped market socket is reopened and the live set is replayed."""

    def _registry(self) -> InstrumentRegistry:
        registry = InstrumentRegistry()
        iid = InstrumentId.equity("NSE", "RELIANCE")
        registry.register(iid, {"key": "NSE:2885", "asset_class": "EQUITY"})
        registry.add_alias("NSE:2885", iid)
        return registry

    def test_socket_drop_reopens_and_resubscribes(self) -> None:
        opened: list[Any] = []

        class FakeWS:
            def __init__(self) -> None:
                self.sent: list[bytes] = []

            def send(self, data: bytes) -> None:
                self.sent.append(data)

            def recv(self) -> bytes:
                raise ConnectionError("socket dropped")

            def close(self) -> None:
                pass

        def factory(url: str) -> FakeWS:
            ws = FakeWS()
            opened.append(ws)
            return ws

        backend = DhanMarketDataStreamBackend(
            token_provider=lambda: "tok",
            client_id="client",
            registry=self._registry(),
            ws_factory=factory,
        )
        # Fast, deterministic backoff for the test.
        backend._reconnect = WSReconnectManager(
            max_retries=200, base_delay=0.01, max_delay=0.02, jitter=False
        )
        backend.subscribe_quotes([Equity.of("NSE", "RELIANCE")], MagicMock())
        assert len(opened) == 1  # single socket, multiplexed
        deadline = time.monotonic() + 5
        while len(opened) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(opened) >= 2  # reconnect opened a fresh socket
        frames = [json.loads(s) for s in opened[1].sent]
        assert any(f.get("RequestCode") == 21 for f in frames)
        assert any(
            str(item.get("SecurityId")) == "2885"
            for f in frames
            for item in f.get("InstrumentList", [])
        )
        backend.close()

    def test_reconnect_disabled_keeps_single_socket(self) -> None:
        opened: list[Any] = []

        class FakeWS:
            def recv(self) -> bytes:
                raise ConnectionError("socket dropped")

            def close(self) -> None:
                pass

        def factory(url: str) -> FakeWS:
            ws = FakeWS()
            opened.append(ws)
            return ws

        backend = DhanMarketDataStreamBackend(
            token_provider=lambda: "tok",
            client_id="client",
            registry=self._registry(),
            ws_factory=factory,
            reconnect=False,
        )
        backend.subscribe_quotes([Equity.of("NSE", "RELIANCE")], MagicMock())
        time.sleep(0.1)  # give a reconnect attempt a chance (it must not run)
        assert len(opened) == 1
        backend.close()

    def _make_fake_factory(self, opened: list[Any]):
        class FakeWS:
            def __init__(self) -> None:
                self.sent: list[bytes] = []

            def send(self, data: bytes) -> None:
                self.sent.append(data)

            def recv(self) -> bytes:
                raise ConnectionError("socket dropped")

            def close(self) -> None:
                pass

        def factory(url: str) -> FakeWS:
            ws = FakeWS()
            opened.append(ws)
            return ws

        return factory

    def _registry_with(self, *pairs: tuple[str, str]) -> InstrumentRegistry:
        registry = InstrumentRegistry()
        for symbol, key in pairs:
            iid = InstrumentId.equity("NSE", symbol)
            registry.register(iid, {"key": key, "asset_class": "EQUITY"})
            registry.add_alias(key, iid)
        return registry

    def test_unsubscribe_instruments_prunes_wire_set(self) -> None:
        """Ghost instruments must not survive unsubscribe or be replayed."""
        opened: list[Any] = []
        backend = DhanMarketDataStreamBackend(
            token_provider=lambda: "tok",
            client_id="client",
            registry=self._registry_with(("RELIANCE", "NSE:2885"), ("TCS", "NSE:2038")),
            ws_factory=self._make_fake_factory(opened),
            reconnect=False,
        )
        reliance = Equity.of("NSE", "RELIANCE")
        tcs = Equity.of("NSE", "TCS")
        backend.subscribe_quotes([reliance, tcs], MagicMock())
        assert len(backend._subscribed_instruments) == 2
        # Drop one instrument — the wire set must prune it so a reconnect
        # never resurrects it.
        backend.unsubscribe_instruments([reliance])
        assert backend._subscribed_instruments == [tcs]
        assert reliance.instrument_id not in backend._instrument_refs
        # A fresh open replays only the surviving instrument.
        backend._ws = None
        backend._ensure_ws()
        frames = [json.loads(s) for s in opened[1].sent]
        assert any(f.get("RequestCode") == 21 for f in frames)
        ids = {
            str(item.get("SecurityId"))
            for f in frames
            for item in f.get("InstrumentList", [])
        }
        assert ids == {"2038"}
        backend.close()

    def test_ensure_ws_replays_full_live_set_on_fresh_open(self) -> None:
        """A fresh manual open heals the pre-outage set (backoff-exhaustion path)."""
        opened: list[Any] = []
        backend = DhanMarketDataStreamBackend(
            token_provider=lambda: "tok",
            client_id="client",
            registry=self._registry_with(("RELIANCE", "NSE:2885"), ("TCS", "NSE:2038")),
            ws_factory=self._make_fake_factory(opened),
            reconnect=False,
        )
        reliance = Equity.of("NSE", "RELIANCE")
        tcs = Equity.of("NSE", "TCS")
        backend.subscribe_quotes([reliance, tcs], MagicMock())
        assert len(opened) == 1
        # Simulate the post-exhaustion state: socket gone, backoff spent.
        backend._reconnect._attempt = backend._reconnect._max_retries
        backend._ws = None
        backend._ensure_ws()
        assert len(opened) == 2
        frames = [json.loads(s) for s in opened[1].sent]
        assert any(f.get("RequestCode") == 21 for f in frames)
        ids = {
            str(item.get("SecurityId"))
            for f in frames
            for item in f.get("InstrumentList", [])
        }
        assert ids == {"2885", "2038"}  # the full pre-outage set was replayed
        assert backend._reconnect.attempt_count == 0  # backoff reset on heal
        backend.close()


# ---------------------------------------------------------------------------
# Order-backend reconnect tests (AutoReconnectMixin)
# ---------------------------------------------------------------------------


class TestOrderBackendReconnect:
    """A dropped Dhan order-update socket is reopened with a fresh token."""

    def _backend(self, opened: list[Any], *, reconnect: bool = True):
        from tradex_brokers.dhan.ws_streams import DhanOrderStreamBackend

        class FakeWS:
            def __init__(self) -> None:
                self.sent: list[bytes] = []

            def send(self, data: bytes) -> None:
                self.sent.append(data)

            def recv(self) -> bytes:
                raise ConnectionError("socket dropped")

            def close(self) -> None:
                pass

        def factory(url: str) -> FakeWS:
            ws = FakeWS()
            opened.append(ws)
            return ws

        backend = DhanOrderStreamBackend(
            token_provider=lambda: "tok",
            client_id="client",
            map_order=lambda row: MagicMock(),
            ws_factory=factory,
            reconnect=reconnect,
        )
        return backend

    def test_socket_drop_reopens_with_fresh_token(self) -> None:
        import time

        opened: list[Any] = []
        backend = self._backend(opened)
        backend._reconnect = WSReconnectManager(
            max_retries=200, base_delay=0.01, max_delay=0.02, jitter=False
        )
        backend.subscribe_orders(MagicMock())
        assert len(opened) == 1
        deadline = time.monotonic() + 5
        while len(opened) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(opened) >= 2  # reconnect opened a fresh socket
        backend.close()

    def test_reconnect_disabled_keeps_single_socket(self) -> None:
        import time

        opened: list[Any] = []
        backend = self._backend(opened, reconnect=False)
        backend.subscribe_orders(MagicMock())
        time.sleep(0.1)  # a reconnect attempt must not run
        assert len(opened) == 1
        backend.close()


# ---------------------------------------------------------------------------
# Registry alias tests
# ---------------------------------------------------------------------------


class TestBareIdAlias:
    def test_load_instruments_registers_bare_id_alias(self):
        broker = DhanBroker()
        broker.load_instruments(
            [
                {
                    "symbol": "RELIANCE",
                    "exchange": "NSE",
                    "key": "NSE:2885",
                    "asset_class": "EQUITY",
                    "security_id": "2885",
                }
            ]
        )
        iid = broker.registry.resolve("2885")
        assert iid is not None
        assert str(iid) == str(InstrumentId.equity("NSE", "RELIANCE"))

    def test_rows_without_security_id_still_load(self):
        broker = DhanBroker()
        broker.load_instruments(
            [{"symbol": "TCS", "exchange": "NSE", "key": "NSE:2038", "asset_class": "EQUITY"}]
        )
        assert broker.registry.resolve("NSE:2038") is not None
        assert broker.registry.resolve("2038") is None  # no bare id to alias
