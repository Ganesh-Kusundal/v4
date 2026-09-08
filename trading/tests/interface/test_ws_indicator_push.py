"""WS indicator push — subscription modes, throttling, and frame shape.

Covers the hybrid contract in
docs/superpowers/specs/2026-09-09-ws-indicator-push-design.md: bar-close
pushes only on closed bars, tick mode throttles forming-bar recomputes,
unknown ids fail the whole subscription loudly, unsub silences, and pushed
points are finite-or-null (never NaN).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from tradex_trading.interface.fastapi_app import create_app
from tradex_trading.interface.routes.stream_indicators import IndicatorStreamRegistry
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.runtime.bar_aggregator import BarFrame


def _make_app():
    bus = ReactiveBus()
    session = MagicMock()
    session.state = "READY"
    session.bus = bus
    return create_app(session=session), bus


def _indicator_sub(ws, instrument="NSE:RELIANCE", interval="1m", ids=None, mode=None):
    item = {"instrument": instrument, "interval": interval, "ids": ids or ["sma"]}
    if mode:
        item["mode"] = mode
    ws.send_text(json.dumps({"type": "indicator-sub", "indicators": [item]}))
    return ws.receive_json()


def _frame(instrument="NSE:RELIANCE", timeframe="1m", time=1784087100, closed=False):
    return BarFrame(
        instrument=instrument, timeframe=timeframe, time=time,
        open=100.0, high=100.5, low=99.5, close=100.2, volume=1000.0,
        closed=closed,
    )


def test_bar_close_pushes_only_on_closed_bars():
    """bar-close mode: forming frame pushes nothing; closed frame pushes."""
    reg = IndicatorStreamRegistry(push=lambda m: _pushed.append(m))
    _pushed = []
    reg.add("NSE:RELIANCE", "1m", ["sma"], "bar-close")
    reg.handle_bar_frame(_frame(closed=False))
    assert _pushed == []
    reg.handle_bar_frame(_frame(closed=True))
    assert len(_pushed) == 1
    msg = _pushed[0]
    assert msg["type"] == "indicator"
    assert msg["instrument"] == "NSE:RELIANCE"
    assert msg["id"] == "sma"
    assert len(msg["points"]) == 2
    for point in msg["points"]:
        assert "time" in point
        assert point["value"] is None or isinstance(point["value"], float)


def test_tick_mode_throttles_and_skips_closed():
    """tick mode: forming frames throttle to 1/s; closed frames ignored."""
    reg = IndicatorStreamRegistry(push=lambda m: _pushed.append(m))
    _pushed = []
    reg.add("NSE:RELIANCE", "1m", ["sma"], "tick")
    reg.handle_bar_frame(_frame(time=1784087100, closed=False))
    assert len(_pushed) == 1
    reg.handle_bar_frame(_frame(time=1784087100, closed=False))
    assert len(_pushed) == 1, "second immediate forming update must throttle"
    reg.handle_bar_frame(_frame(time=1784087100, closed=True))
    assert len(_pushed) == 1, "tick mode ignores closed frames"


def test_unknown_id_rejects_whole_subscription():
    reg = IndicatorStreamRegistry(push=lambda m: None)
    with pytest.raises(ValueError, match="unknown indicators"):
        reg.add("NSE:RELIANCE", "1m", ["sma", "no-such-indicator"], "bar-close")
    assert not reg.has_subs("NSE:RELIANCE", "1m"), "partial registration forbidden"


def test_bad_mode_rejected():
    reg = IndicatorStreamRegistry(push=lambda m: None)
    with pytest.raises(ValueError, match="bad mode"):
        reg.add("NSE:RELIANCE", "1m", ["sma"], "every-tick")


def test_unsub_silences_and_scopes():
    reg = IndicatorStreamRegistry(push=lambda m: _pushed.append(m))
    _pushed = []
    reg.add("NSE:RELIANCE", "1m", ["sma", "ema"], "bar-close")
    reg.remove("NSE:RELIANCE", "1m", ["ema"])
    reg.handle_bar_frame(_frame(closed=True))
    assert [m["id"] for m in _pushed] == ["sma"]
    reg.remove("NSE:RELIANCE", "1m", None)
    assert not reg.has_subs("NSE:RELIANCE", "1m")
    reg.handle_bar_frame(_frame(closed=True))
    assert [m["id"] for m in _pushed] == ["sma"]


def test_ws_indicator_sub_ack_and_error():
    app, _bus = _make_app()
    client = TestClient(app)
    with client.websocket_connect("/ws/stream") as ws:
        ack = _indicator_sub(ws, ids=["sma"])
        assert ack["type"] == "indicator-subscribed"
        assert ack["indicators"][0]["ids"] == ["sma"]
        assert ack["indicators"][0]["mode"] == "bar-close"
        bad = _indicator_sub(ws, ids=["bogus-id"])
        assert bad["type"] == "error"
        assert "bogus-id" in bad["message"]


def test_ws_indicator_unsub_ack():
    app, _bus = _make_app()
    client = TestClient(app)
    with client.websocket_connect("/ws/stream") as ws:
        _indicator_sub(ws, ids=["sma", "ema"])
        ws.send_text(json.dumps({
            "type": "indicator-unsub",
            "indicators": [{"instrument": "NSE:RELIANCE", "interval": "1m"}],
        }))
        ack = ws.receive_json()
        assert ack["type"] == "indicator-unsubscribed"
        assert sorted(ack["indicators"][0]["ids"]) == ["ema", "sma"]
