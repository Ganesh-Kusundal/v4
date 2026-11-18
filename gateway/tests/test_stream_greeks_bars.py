from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from gateway_support import FakeBroker, gateway_config, option
from tradex_domain.market import Quote
from tradex_domain.value_objects import Price, Quantity
from tradex_gateway.app import create_app
from tradex_gateway.brokers import BrokerRuntime


def _receive_op(ws, op: str, limit: int = 10) -> dict:
    for _ in range(limit):
        frame = ws.receive_json()
        if frame.get("op") == op:
            return frame
    raise AssertionError(f"no {op!r} frame within {limit} frames")


class _GreeksFakeBroker(FakeBroker):
    def __init__(self, name: str = "dhan") -> None:
        super().__init__(name)
        self.greeks_sub_calls = 0
        self.greeks_handlers: list = []

    def subscribe_option_greeks(self, instruments, handler):
        self.greeks_sub_calls += 1
        self.greeks_handlers.append(handler)
        return f"{self.name}-greeks-{self.greeks_sub_calls}"


def _app(dhan_broker=None):
    dhan = dhan_broker or FakeBroker("dhan")
    upstox = FakeBroker("upstox")
    app = create_app(
        gateway_config(),
        {
            "dhan": BrokerRuntime("dhan", dhan),
            "upstox": BrokerRuntime("upstox", upstox),
        },
    )
    return app, dhan, upstox


def _quote_with_greeks(inst, *, ltp: float = 125.5) -> Quote:
    level = Price(Decimal(str(ltp)))
    return Quote(
        instrument=inst,
        ltp=level,
        bid=level,
        ask=Price(Decimal(str(ltp + 0.1))),
        volume=Quantity(Decimal("500")),
        open_interest=Quantity(Decimal("10000")),
        timestamp=datetime(2026, 11, 6, 3, 45, 10, tzinfo=UTC),
        metadata={
            "greeks": {"delta": 0.55, "gamma": 0.02, "theta": -12.3, "vega": 8.4},
            "iv": 18.4,
        },
    )


def test_stream_option_greeks_uses_dedicated_handler_and_emits_greeks_and_iv():
    opt = option()
    opt_id = str(opt.instrument_id)
    broker = _GreeksFakeBroker("dhan")
    app, dhan, _ = _app(broker)

    with TestClient(app) as client, client.websocket_connect("/v1/stream") as ws:
        _receive_op(ws, "status")
        ws.send_json({"op": "subscribe", "instruments": [opt_id], "mode": "option_greeks"})
        ack = _receive_op(ws, "ack")
        assert ack["mode"] == "option_greeks"
        assert ack["instruments"] == [opt_id]
        assert dhan.greeks_sub_calls == 1
        assert dhan.quote_sub_calls == 0

        dhan.greeks_handlers[0](_quote_with_greeks(opt))
        frame = _receive_op(ws, "quote")
        assert frame["instrument"] == opt_id
        assert frame["greeks"] == {"delta": 0.55, "gamma": 0.02, "theta": -12.3, "vega": 8.4}
        assert frame["iv"] == 18.4


def test_stream_option_greeks_falls_back_to_subscribe_quotes_when_unavailable():
    opt = option()
    opt_id = str(opt.instrument_id)
    app, dhan, _ = _app()

    with TestClient(app) as client, client.websocket_connect("/v1/stream") as ws:
        _receive_op(ws, "status")
        ws.send_json({"op": "subscribe", "instruments": [opt_id], "mode": "option_greeks"})
        ack = _receive_op(ws, "ack")
        assert ack["mode"] == "option_greeks"
        assert dhan.quote_sub_calls == 1

        dhan.quote_handlers[0](_quote_with_greeks(opt, ltp=130.0))
        frame = _receive_op(ws, "quote")
        assert frame["instrument"] == opt_id
        assert frame["ltp"] == 130.0
        assert frame["greeks"]["delta"] == 0.55
        assert frame["iv"] == 18.4


def test_stream_bars_emits_forming_and_closed_bars():
    app, dhan, _ = _app()
    inst = dhan.universe[0]  # NSE:RELIANCE
    inst_id = str(inst.instrument_id)

    with TestClient(app) as client, client.websocket_connect("/v1/stream") as ws:
        _receive_op(ws, "status")
        ws.send_json(
            {
                "op": "subscribe",
                "instruments": [inst_id],
                "mode": "bars",
                "timeframe": "1m",
            }
        )
        ack = _receive_op(ws, "ack")
        assert ack["mode"] == "bars"
        assert ack["timeframe"] == "1m"
        assert dhan.quote_sub_calls == 1

        # Tick 1 in 09:15 IST (03:45:10 UTC) -> forming bar
        dhan.quote_handlers[0](
            Quote(
                instrument=inst,
                ltp=Price(Decimal("100.0")),
                volume=Quantity(Decimal("10")),
                timestamp=datetime(2026, 11, 6, 3, 45, 10, tzinfo=UTC),
            )
        )
        forming_1 = _receive_op(ws, "bar")
        assert forming_1["instrument"] == inst_id
        assert forming_1["timeframe"] == "1m"
        assert forming_1["o"] == 100.0
        assert forming_1["h"] == 100.0
        assert forming_1["l"] == 100.0
        assert forming_1["c"] == 100.0
        assert forming_1["v"] == 10.0
        assert forming_1["closed"] is False
        t0 = forming_1["t"]

        # Tick 2 in 09:16 IST (03:46:05 UTC) -> closes 09:15 bar and opens 09:16 forming bar
        dhan.quote_handlers[0](
            Quote(
                instrument=inst,
                ltp=Price(Decimal("102.5")),
                volume=Quantity(Decimal("25")),
                timestamp=datetime(2026, 11, 6, 3, 46, 5, tzinfo=UTC),
            )
        )
        closed_bar = _receive_op(ws, "bar")
        assert closed_bar["closed"] is True
        assert closed_bar["t"] == t0
        assert closed_bar["o"] == 100.0
        assert closed_bar["c"] == 100.0
        assert closed_bar["v"] == 10.0

        forming_2 = _receive_op(ws, "bar")
        assert forming_2["closed"] is False
        assert forming_2["t"] == t0 + 60
        assert forming_2["o"] == 102.5
        assert forming_2["c"] == 102.5
        assert forming_2["v"] == 25.0


def test_stream_bars_refcounted_dedupe_across_two_clients():
    app, dhan, _ = _app()
    inst = dhan.universe[0]
    inst_id = str(inst.instrument_id)

    with (
        TestClient(app) as client,
        client.websocket_connect("/v1/stream") as ws1,
        client.websocket_connect("/v1/stream") as ws2,
    ):
        _receive_op(ws1, "status")
        _receive_op(ws2, "status")

        ws1.send_json({"op": "subscribe", "instruments": [inst_id], "mode": "bars", "timeframe": "1m"})
        ack1 = _receive_op(ws1, "ack")
        ws2.send_json({"op": "subscribe", "instruments": [inst_id], "mode": "bars", "timeframe": "1m"})
        ack2 = _receive_op(ws2, "ack")

        # Only one upstream quote subscription is opened for both clients.
        assert dhan.quote_sub_calls == 1

        dhan.quote_handlers[0](
            Quote(
                instrument=inst,
                ltp=Price(Decimal("250.0")),
                volume=Quantity(Decimal("15")),
                timestamp=datetime(2026, 11, 6, 3, 45, 10, tzinfo=UTC),
            )
        )
        bar1 = _receive_op(ws1, "bar")
        bar2 = _receive_op(ws2, "bar")
        assert bar1 == bar2
        assert bar1["o"] == 250.0
        assert bar1["closed"] is False

        ws2.send_json({"op": "unsubscribe", "subscription_id": ack2["subscription_id"]})
        _receive_op(ws2, "ack")
        assert dhan.unsubscribed == []

        ws1.send_json({"op": "unsubscribe", "subscription_id": ack1["subscription_id"]})
        _receive_op(ws1, "ack")
        assert inst_id in dhan.unsubscribed


def test_stream_bars_rejects_invalid_timeframe():
    app, dhan, _ = _app()
    inst_id = str(dhan.universe[0].instrument_id)

    with TestClient(app) as client, client.websocket_connect("/v1/stream") as ws:
        _receive_op(ws, "status")
        ws.send_json(
            {
                "op": "subscribe",
                "instruments": [inst_id],
                "mode": "bars",
                "timeframe": "2m",
            }
        )
        err = _receive_op(ws, "error")
        assert err["code"] == "invalid_request"
        ws.send_json({"op": "ping"})
        pong = _receive_op(ws, "pong")
        assert pong["op"] == "pong"
