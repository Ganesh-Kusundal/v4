"""BarFrame.source tagging: live quotes emit 'live', replay-forwarded quotes
pass source='sim' through on_quote so the wire frame distinguishes them."""

from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from tradex_domain.enums import Timeframe

from tradex_trading.runtime.bar_aggregator import BarAggregator

IST = ZoneInfo("Asia/Kolkata")


def _agg(frames: list) -> BarAggregator:
    return BarAggregator(
        "NSE:TEST",
        Timeframe.M1,
        on_frame=frames.append,
        now=lambda: 0.0,  # never throttle forming frames in tests
    )


def test_live_on_quote_emits_source_live():
    frames: list = []
    agg = _agg(frames)
    agg.on_quote(datetime(2026, 7, 15, 10, 0, 0, tzinfo=IST), Decimal("100"), Decimal("1"))
    assert frames and frames[-1].source == "live"


def test_replay_quote_emits_source_sim():
    frames: list = []
    agg = _agg(frames)
    agg.on_quote(
        datetime(2026, 7, 15, 10, 0, 0, tzinfo=IST),
        Decimal("100"),
        Decimal("1"),
        source="sim",
    )
    assert frames and frames[-1].source == "sim"
    assert frames[-1].closed is False


def test_wire_frame_carries_live_source():
    """A quote published on a real session bus reaches the WS as source='live'
    (paper mode has no market feed, so this drives the bus directly)."""
    fastapi = pytest.importorskip("fastapi")

    from fastapi.testclient import TestClient
    from tradex_domain.instruments import Equity
    from tradex_domain.market import Quote
    from tradex_domain.value_objects import Price

    from tradex_trading.interface.fastapi_app import create_app
    from tradex_trading.reactive.bus import ReactiveBus

    bus = ReactiveBus()
    session = MagicMock()
    session.state = "READY"
    session.bus = bus
    app = create_app(session=session)
    inst = Equity.of("NSE", "TEST")
    ts = datetime(2026, 7, 15, 10, 0, 0, tzinfo=IST)

    with TestClient(app).websocket_connect("/ws/stream") as ws:
        ws.send_json({
            "type": "subscribe_bars",
            "bars": [{"instrument": "NSE:TEST", "interval": "1m"}],
        })
        assert ws.receive_json()["type"] == "subscribed_bars"
        bus.publish(Quote(instrument=inst, ltp=Price(Decimal("100")), timestamp=ts))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            frame = ws.receive_json()
            if frame.get("type") == "bar":
                assert frame["source"] == "live"
                return
        pytest.fail("no bar frame arrived")
