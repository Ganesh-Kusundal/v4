"""Depth stream conformance with openalgo-charts 2.1.0.

Contract source: openalgo-charts/src/feed/types.ts:28-42 —
``MarketDepth = {bids: [{price, qty, orders?}], asks: [...], ltp, ltq?}``,
object levels, numeric values, ltp required. The backend's ``/ws/stream``
``depth`` frames must parse against that shape: a conformance test, not a
feature (see docs/superpowers/specs/2026-09-09-depth-conformance-design.md).
"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from tradex_trading.interface.fastapi_app import create_app
from tradex_trading.reactive.bus import ReactiveBus


def _make_session():
    bus = ReactiveBus()
    session = MagicMock()
    session.state = "READY"
    session.bus = bus
    return session, bus


def _subscribe_depth(ws, instrument: str = "NSE:RELIANCE", depth: str = "30"):
    ws.send_text(json.dumps({
        "type": "subscribe",
        "instruments": [instrument],
        "depth": depth,
    }))
    return ws.receive_json()


def _publish_depth(bus, instrument: str = "NSE:RELIANCE"):
    from tradex_domain.instruments import Equity
    from tradex_domain.market import Depth
    from tradex_domain.value_objects import Price, Quantity

    bus.publish(Depth(
        instrument=Equity.of("NSE", "RELIANCE" if instrument == "NSE:RELIANCE" else "TCS"),
        bids=(
            (Price(value=Decimal("1319.0")), Quantity(value=Decimal("10"))),
            (Price(value=Decimal("1318.5")), Quantity(value=Decimal("20"))),
        ),
        asks=(
            (Price(value=Decimal("1319.5")), Quantity(value=Decimal("5"))),
            (Price(value=Decimal("1320.0")), Quantity(value=Decimal("8"))),
        ),
    ))


def test_depth_frame_parses_as_engine_market_depth():
    """Every level object is numeric, ltp present, sides correctly ordered."""
    session, bus = _make_session()
    app = create_app(session=session)
    client = TestClient(app)

    with client.websocket_connect("/ws/stream") as ws:
        ack = _subscribe_depth(ws)
        assert ack["type"] == "subscribed"
        _publish_depth(bus)
        msg = ws.receive_json()
        assert msg["type"] == "depth"
        assert msg["instrument"] == "NSE:RELIANCE"

        bids, asks, ltp = msg["bids"], msg["asks"], msg["ltp"]
        assert isinstance(ltp, int | float)
        for level in bids + asks:
            assert set(level) == {"price", "qty"}
            assert isinstance(level["price"], float)
            assert isinstance(level["qty"], float)
            assert level["price"] > 0
            assert level["qty"] > 0
        assert [b["price"] for b in bids] == sorted(
            (b["price"] for b in bids), reverse=True
        ), "bids must be price-descending"
        assert [a["price"] for a in asks] == sorted(a["price"] for a in asks), (
            "asks must be price-ascending"
        )
        # LTP: quote seen first, else best-bid fallback.
        assert ltp == bids[0]["price"]


def test_depth_ltp_prefers_quote_stream():
    """When a quote has been seen, depth ltp is the quote LTP, not best bid."""
    session, bus = _make_session()
    app = create_app(session=session)
    client = TestClient(app)

    with client.websocket_connect("/ws/stream") as ws:
        _subscribe_depth(ws)
        from tradex_domain.instruments import Equity
        from tradex_domain.market import Quote
        from tradex_domain.value_objects import Price

        bus.publish(Quote(
            instrument=Equity.of("NSE", "RELIANCE"),
            ltp=Price(value=Decimal("1321.75")),
        ))
        # quote frame arrives first; consume it
        quote_msg = ws.receive_json()
        assert quote_msg["type"] == "quote"
        _publish_depth(bus)
        msg = ws.receive_json()
        assert msg["ltp"] == 1321.75


def test_depth_off_silences_depth_frames():
    """depth:"off" mutes depth forwarding; an unsubscribe still works, proving
    the socket stays healthy with depth muted (the muted frame is dropped)."""
    session, bus = _make_session()
    app = create_app(session=session)
    client = TestClient(app)

    with client.websocket_connect("/ws/stream") as ws:
        ack = _subscribe_depth(ws, depth="off")
        assert ack["type"] == "subscribed"
        assert ack["depth"] == "off"
        _publish_depth(bus)
        # No depth frame may arrive. Prove it by unsubscribing: the ack is
        # the next frame — if the muted depth had leaked, we'd get it instead.
        ws.send_text(json.dumps({
            "type": "unsubscribe",
            "instruments": ["NSE:RELIANCE"],
        }))
        reply = ws.receive_json()
        assert reply["type"] == "unsubscribed"
