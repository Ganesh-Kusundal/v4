"""Tests for the FastAPI application."""

from unittest.mock import MagicMock, patch

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from tradex_trading.interface.fastapi_app import (  # noqa: E402
    AccountResponse,
    ErrorResponse,
    HealthResponse,
    OrderResponse,
    PositionResponse,
    create_app,
    start_fastapi_server,
)

# ---------------------------------------------------------------------------
# Health endpoints
# ---------------------------------------------------------------------------

def test_health():
    app = create_app()
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_live():
    app = create_app()
    client = TestClient(app)
    r = client.get("/health/live")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["check"] == "live"


def test_health_ready():
    app = create_app()
    client = TestClient(app)
    r = client.get("/health/ready")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["check"] == "ready"


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def test_positions_no_session():
    app = create_app()
    client = TestClient(app)
    r = client.get("/positions")
    assert r.status_code == 200
    assert r.json() == []


def test_positions_with_filter():
    app = create_app()
    client = TestClient(app)
    r = client.get("/positions?instrument=NSE")
    assert r.status_code == 200
    assert r.json() == []


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def test_list_orders_no_session():
    app = create_app()
    client = TestClient(app)
    r = client.get("/orders")
    assert r.status_code == 200
    assert r.json() == []


def test_list_orders_pagination_params():
    app = create_app()
    client = TestClient(app)
    r = client.get("/orders?status=open&limit=10&offset=5")
    assert r.status_code == 200
    assert r.json() == []


def test_get_order_no_session():
    app = create_app()
    client = TestClient(app)
    r = client.get("/orders/abc123")
    assert r.status_code == 404


def test_place_order_no_session():
    app = create_app()
    client = TestClient(app)
    r = client.post("/orders", json={"symbol": "RELIANCE"})
    assert r.status_code == 503


def test_modify_order_no_session():
    app = create_app()
    client = TestClient(app)
    r = client.put("/orders/abc123", json={"quantity": 10})
    assert r.status_code == 400


def test_modify_order_end_to_end_paper_session():
    """PUT /orders/{id} on a live paper session: resolves the instrument,
    routes through the engine spine, and projects the change into the OMS."""
    from decimal import Decimal

    from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
    from tradex_domain.execution import Order
    from tradex_domain.instruments import Equity
    from tradex_domain.value_objects import OrderId, Price, Quantity
    from tradex_trading.sdk.session import TradingSession

    session = TradingSession.paper()
    try:
        # Register RELIANCE in the paper universe and seed an open order.
        reliance = Equity.of("NSE", "RELIANCE")
        session.broker.set_quote(reliance, ltp=Price(value=Decimal("2500")))
        oid = OrderId(value="e2e-1")
        session.engine.cache.update_order(
            Order(
                order_id=oid,
                instrument=reliance,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=Quantity(value=Decimal("10")),
                price=Price(value=Decimal("2500.00")),
                time_in_force=TimeInForce.DAY,
                status=OrderStatus.ACK,
            )
        )

        client = TestClient(create_app(session=session))
        r = client.put(
            "/orders/e2e-1",
            json={
                "symbol": "RELIANCE",
                "side": "BUY",
                "order_type": "LIMIT",
                "quantity": 7,
                "price": "2450.00",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["order_id"] == "e2e-1"
        assert body["status"] == "modified"
        cached = session.engine.cache.get_order("e2e-1")
        assert cached.price.value == Decimal("2450.00")
        assert cached.quantity.value == Decimal("7")
    finally:
        session.stop()


def test_cancel_order_no_session():
    app = create_app()
    client = TestClient(app)
    r = client.delete("/orders/abc123")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

def test_account_no_session():
    app = create_app()
    client = TestClient(app)
    r = client.get("/account")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def test_get_quote_no_session():
    app = create_app()
    client = TestClient(app)
    r = client.get("/quotes/NSE:RELIANCE")
    assert r.status_code == 404


def test_search_instruments_no_session():
    app = create_app()
    client = TestClient(app)
    r = client.get("/search?q=RELIANCE")
    assert r.status_code == 200
    assert r.json() == []


# ---------------------------------------------------------------------------
# Pydantic model serialization
# ---------------------------------------------------------------------------

def test_health_response_model():
    resp = HealthResponse(status="ok")
    data = resp.model_dump(exclude_none=True)
    assert data == {"status": "ok"}


def test_position_response_model():
    resp = PositionResponse(
        instrument="NSE:RELIANCE",
        quantity="100",
        avg_price="2500.00",
        realized_pnl="500.00",
        unrealized_pnl="200.00",
        total_pnl="700.00",
        is_long=True,
        is_short=False,
    )
    data = resp.model_dump()
    assert data["instrument"] == "NSE:RELIANCE"
    assert data["is_long"] is True
    assert data["is_short"] is False


def test_order_response_model():
    resp = OrderResponse(order_id="ord-1", status="accepted", message="test")
    data = resp.model_dump()
    assert data["order_id"] == "ord-1"
    assert data["status"] == "accepted"
    assert data["message"] == "test"


def test_error_response_model():
    resp = ErrorResponse(error="something went wrong")
    assert resp.model_dump() == {"error": "something went wrong"}


def test_account_response_model():
    resp = AccountResponse(balance="10000", margin="5000", equity="15000")
    data = resp.model_dump()
    assert data["balance"] == "10000"
    assert data["margin"] == "5000"
    assert data["equity"] == "15000"


# ---------------------------------------------------------------------------
# API key authentication
# ---------------------------------------------------------------------------

def test_auth_no_key_configured():
    """When no api_key is set, POST should succeed (no auth required)."""
    app = create_app()
    client = TestClient(app)
    r = client.post("/orders", json={"symbol": "RELIANCE"})
    # 503 because no session, not 403
    assert r.status_code == 503


def test_auth_key_configured_rejects_missing():
    """When api_key is set, requests without the key should be rejected."""
    app = create_app(api_key="secret-key")
    client = TestClient(app)
    r = client.post("/orders", json={"symbol": "RELIANCE"})
    assert r.status_code == 403


def test_auth_key_configured_rejects_wrong():
    """When api_key is set, wrong key should be rejected."""
    app = create_app(api_key="secret-key")
    client = TestClient(app)
    r = client.post("/orders", json={"symbol": "RELIANCE"}, headers={"X-API-Key": "wrong"})
    assert r.status_code == 403


def test_auth_key_configured_accepts_correct():
    """When api_key is set, correct key should pass auth."""
    app = create_app(api_key="secret-key")
    client = TestClient(app)
    r = client.post("/orders", json={"symbol": "RELIANCE"}, headers={"X-API-Key": "secret-key"})
    # 503 because no session, not 403 — auth passed
    assert r.status_code == 503


def test_auth_get_endpoints_no_key_needed():
    """GET endpoints should work without API key even when auth is configured."""
    app = create_app(api_key="secret-key")
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    r = client.get("/orders")
    assert r.status_code == 200
    r = client.get("/positions")
    assert r.status_code == 200


def test_auth_delete_requires_key():
    """DELETE should require API key when configured."""
    app = create_app(api_key="secret-key")
    client = TestClient(app)
    r = client.delete("/orders/abc")
    assert r.status_code == 403


def test_auth_put_requires_key():
    """PUT should require API key when configured."""
    app = create_app(api_key="secret-key")
    client = TestClient(app)
    r = client.put("/orders/abc", json={"qty": 1})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------

def test_openapi_docs():
    app = create_app()
    client = TestClient(app)
    r = client.get("/docs")
    assert r.status_code == 200


def test_openapi_json():
    app = create_app()
    client = TestClient(app)
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert "paths" in r.json()


# ---------------------------------------------------------------------------
# Option / future chains
# ---------------------------------------------------------------------------


def _make_chain_session():
    """Mock session whose registry carries MCX:GOLD and whose market serves chains."""
    from datetime import date
    from decimal import Decimal
    from unittest.mock import MagicMock

    from tradex_domain.instruments import Equity, Future, Option
    from tradex_domain.options import Expiry, OptionChain, OptionPair
    from tradex_domain.value_objects import Price
    from tradex_domain.wire import InstrumentRegistry

    reg = InstrumentRegistry()
    gold = Equity.of("MCX", "GOLD")
    reg.register(gold.instrument_id, {"key": "MCX:GOLD"})
    reg.add_alias("GOLD", gold.instrument_id)

    broker = MagicMock()
    broker.registry = reg
    session = MagicMock()
    session._broker = broker

    ce = Option.of("MCX", "GOLD", date(2026, 10, 30), 120000, "CE")
    pe = Option.of("MCX", "GOLD", date(2026, 10, 30), 120000, "PE")
    chain = OptionChain(
        underlying=Equity.of("MCX", "GOLD"),
        _expiries=(
            Expiry(
                underlying=Equity.of("MCX", "GOLD"),
                expiry_date=date(2026, 10, 30),
                pairs=(
                    OptionPair(call=ce, put=pe, strike=Price(value=Decimal("120000"))),
                ),
            ),
        ),
    )
    session.broker.get_option_chain.return_value = chain
    session.broker.future_chain.return_value = [
        Future.of("MCX", "GOLD", date(2026, 9, 4)),
        Future.of("MCX", "GOLD", date(2026, 10, 30)),
    ]
    session.broker.search.return_value = []
    return session


def test_option_chain_endpoint_resolves_and_serializes():
    """/option-chain resolves EXCHANGE:SYMBOL and returns CE/PE pairs."""
    session = _make_chain_session()
    app = create_app(session=session)
    client = TestClient(app)

    r = client.get("/option-chain/MCX:GOLD")
    assert r.status_code == 200
    data = r.json()
    assert data["underlying"] == "MCX:GOLD"
    assert len(data["expiries"]) == 1
    exp = data["expiries"][0]
    assert exp["expiry"] == "2026-10-30"
    assert len(exp["pairs"]) == 1
    assert exp["pairs"][0]["strike"] == "120000"
    assert "call" in exp["pairs"][0]
    assert "put" in exp["pairs"][0]


def test_option_chain_endpoint_bare_symbol_uses_registry():
    """A bare symbol resolves through the broker registry alias."""
    session = _make_chain_session()
    app = create_app(session=session)
    client = TestClient(app)

    r = client.get("/option-chain/GOLD")
    assert r.status_code == 200
    assert r.json()["underlying"] == "MCX:GOLD"


def test_option_chain_endpoint_unknown_underlying_404():
    """An unresolvable underlying yields 404, not 500."""
    session = _make_chain_session()
    app = create_app(session=session)
    client = TestClient(app)

    r = client.get("/option-chain/UNKNOWNTHING")
    assert r.status_code == 404


def test_option_chain_endpoint_passes_expiry_filter():
    """The optional expiry query param reaches market.option_chain."""
    session = _make_chain_session()
    app = create_app(session=session)
    client = TestClient(app)

    r = client.get("/option-chain/MCX:GOLD?expiry=2026-10-30")
    assert r.status_code == 200
    session.broker.get_option_chain.assert_called()
    _inst, expiry = session.broker.get_option_chain.call_args.args
    assert expiry == "2026-10-30"


def test_future_chain_endpoint():
    """/future-chain returns the master-derived future contracts, expiry-sorted."""
    session = _make_chain_session()
    app = create_app(session=session)
    client = TestClient(app)

    r = client.get("/future-chain/MCX:GOLD")
    assert r.status_code == 200
    data = r.json()
    assert data["underlying"] == "MCX:GOLD"
    expiries = [f["expiry"] for f in data["futures"]]
    assert expiries == ["2026-09-04", "2026-10-30"]


def test_future_chain_endpoint_unknown_underlying_404():
    """An unresolvable underlying yields 404 for futures too."""
    session = _make_chain_session()
    app = create_app(session=session)
    client = TestClient(app)

    r = client.get("/future-chain/UNKNOWNTHING")
    assert r.status_code == 404


def test_option_chain_live_mcx_keeps_ltp_window_no_batch():
    """MCX live enrichment skips batch quotes and returns per-leg LTP only."""
    from decimal import Decimal

    from tradex_domain.value_objects import Price

    session = _make_chain_session()
    session.broker.ltp.return_value = Price(value=Decimal("120100"))
    app = create_app(session=session)
    client = TestClient(app)

    r = client.get("/option-chain/MCX:GOLD?live=true")
    assert r.status_code == 200
    data = r.json()
    assert data["live"] is True
    pair = data["expiries"][0]["pairs"][0]
    # MCX: no OI/volume (no REST batch quotes); only the ATM LTP window.
    assert pair["call_live"] == {"ltp": "120100"}
    assert pair["put_live"] == {"ltp": "120100"}
    # quote_batch must NOT have been attempted for MCX.
    session.broker.quote_batch.assert_not_called()


def _make_nfo_chain_session():
    """NFO NIFTY chain whose quote_batch returns greeks-carrying quotes."""
    from datetime import date
    from decimal import Decimal

    from tradex_domain.instruments import Equity, Option
    from tradex_domain.market import Quote
    from tradex_domain.options import Expiry, OptionChain, OptionPair
    from tradex_domain.value_objects import Price
    from tradex_domain.wire import InstrumentRegistry

    reg = InstrumentRegistry()
    nifty = Equity.of("IDX", "NIFTY")
    reg.register(nifty.instrument_id, {"key": "IDX:NIFTY"})
    reg.add_alias("NIFTY", nifty.instrument_id)

    broker = MagicMock()
    broker.registry = reg
    session = MagicMock()
    session._broker = broker

    strikes = [Decimal("23000"), Decimal("23500")]
    pairs = []
    for strike in strikes:
        ce = Option.of("NFO", "NIFTY", date(2026, 10, 29), strike, "CE")
        pe = Option.of("NFO", "NIFTY", date(2026, 10, 29), strike, "PE")
        pairs.append(OptionPair(call=ce, put=pe, strike=Price(value=strike)))
    chain = OptionChain(
        underlying=nifty,
        _expiries=(
            Expiry(
                underlying=nifty,
                expiry_date=date(2026, 10, 29),
                pairs=tuple(pairs),
                reference_price=Price(value=Decimal("23200")),
            ),
        ),
    )
    session.broker.get_option_chain.return_value = chain
    session.broker.search.return_value = []

    def _quote(instrument, ltp, delta):
        return Quote(
            instrument=instrument,
            ltp=Price(value=Decimal(ltp)),
            metadata={"greeks": {"delta": delta, "gamma": 0.01, "theta": -5.0,
                                  "vega": 8.0, "rho": 0.02}},
        )

    session.broker.quote_batch.return_value = {
        pairs[0].call.instrument_id: _quote(pairs[0].call, "45.5", 0.62),
        pairs[0].put.instrument_id: _quote(pairs[0].put, "38.2", -0.38),
    }
    return session


def test_option_chain_live_nfo_attaches_greeks_and_oi():
    """NFO live enrichment attaches greeks/oi/volume from batch quotes."""
    session = _make_nfo_chain_session()
    app = create_app(session=session)
    client = TestClient(app)

    r = client.get("/option-chain/NIFTY?live=true")
    assert r.status_code == 200
    data = r.json()
    assert data["live"] is True
    pairs = data["expiries"][0]["pairs"]
    # ATM-centred window picks the nearest strike; both legs get live data.
    any_call = any(p.get("call_live") for p in pairs)
    assert any_call
    for p in pairs:
        leg = p.get("call_live") or p.get("put_live")
        if leg is not None:
            assert "greeks" in leg
            assert leg["greeks"]["delta"] in (0.62, -0.38)
            break


# ---------------------------------------------------------------------------
# POST /orders — wired to session.trade.submit()
# ---------------------------------------------------------------------------


def _make_mock_session():
    """Build a minimal mock TradingSession for order tests."""
    from tradex_domain.enums import OrderStatus
    from tradex_domain.execution import OrderReceipt
    from tradex_domain.value_objects import OrderId

    session = MagicMock()
    session.state = "READY"
    receipt = OrderReceipt(
        order_id=OrderId("ORD-001"),
        status=OrderStatus.SUBMITTED,
        message="submitted",
    )
    session.trade.submit.return_value = receipt
    return session


def test_post_order_with_session():
    session = _make_mock_session()
    app = create_app(session=session)
    client = TestClient(app)

    body = {
        "exchange": "NSE",
        "symbol": "RELIANCE",
        "side": "BUY",
        "quantity": 10,
        "order_type": "MARKET",
    }
    r = client.post("/orders", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["order_id"] == "ORD-001"
    assert data["status"] == "SUBMITTED"
    assert data["message"] == "submitted"
    session.trade.submit.assert_called_once()


def test_post_order_with_instrument_id():
    session = _make_mock_session()
    app = create_app(session=session)
    client = TestClient(app)

    body = {
        "instrument_id": "NSE:RELIANCE",
        "side": "SELL",
        "quantity": 5,
    }
    r = client.post("/orders", json=body)
    assert r.status_code == 200
    assert r.json()["order_id"] == "ORD-001"


def test_post_order_missing_fields():
    session = _make_mock_session()
    app = create_app(session=session)
    client = TestClient(app)

    body = {"exchange": "NSE", "symbol": "REL"}
    r = client.post("/orders", json=body)
    assert r.status_code == 422


def test_post_order_missing_instrument():
    session = _make_mock_session()
    app = create_app(session=session)
    client = TestClient(app)

    body = {"side": "BUY", "quantity": 1}
    r = client.post("/orders", json=body)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# WebSocket — ReactiveBus bridge
# ---------------------------------------------------------------------------


def test_websocket_no_session():
    app = create_app(session=None)
    client = TestClient(app)
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/stream"):
            pass  # should be rejected


def test_websocket_connect_and_disconnect():
    """WebSocket connects when session is present and cleans up on disconnect."""
    from tradex_trading.reactive.bus import ReactiveBus

    bus = ReactiveBus()
    session = MagicMock()
    session.state = "READY"
    session.bus = bus

    app = create_app(session=session)
    client = TestClient(app)

    with client.websocket_connect("/ws/stream") as ws:
        ws.send_text("ping")
    # Exiting the context triggers disconnect + disposable cleanup


def test_websocket_forwards_depth():
    """Depth events for subscribed instruments are forwarded over /ws/stream."""
    import json as _json
    from decimal import Decimal

    from tradex_domain.instruments import Equity
    from tradex_domain.market import Depth
    from tradex_domain.value_objects import Price, Quantity

    from tradex_trading.reactive.bus import ReactiveBus

    bus = ReactiveBus()
    session = MagicMock()
    session.state = "READY"
    session.bus = bus

    app = create_app(session=session)
    client = TestClient(app)

    with client.websocket_connect("/ws/stream") as ws:
        ws.send_text(_json.dumps({
            "type": "subscribe",
            "instruments": ["NSE:RELIANCE"],
            "depth": "30",
        }))
        ack = ws.receive_json()
        assert ack["type"] == "subscribed"
        depth = Depth(
            instrument=Equity.of("NSE", "RELIANCE"),
            bids=((Price(value=Decimal("1319.0")), Quantity(value=Decimal("10"))),),
            asks=((Price(value=Decimal("1319.5")), Quantity(value=Decimal("5"))),),
        )
        bus.publish(depth)
        msg = ws.receive_json()
        assert msg["type"] == "depth"
        assert msg["instrument"] == "NSE:RELIANCE"
        assert msg["bids"] == [["1319.0", "10"]]
        assert msg["asks"] == [["1319.5", "5"]]
        assert msg["levels"] == 2


def test_websocket_depth_gates_non_nse():
    """Depth on a non-NSE exchange acks a loud error and keeps the socket open."""
    import json as _json

    from tradex_trading.reactive.bus import ReactiveBus
    from tradex_trading.runtime.market_feed import MarketFeed

    class _FakeFeedBroker:
        """Minimal broker surface the feed needs (Dhan depth-20 path)."""

        def subscribe_quotes(self, instruments, handler):
            return "q-sub"

        def subscribe_depth(self, instruments, handler):
            return "d-sub"

        def unsubscribe(self, subscription):
            return None

        @property
        def depth_levels(self):
            return 20

        @property
        def max_stream_instruments(self):
            return 100

    bus = ReactiveBus()
    feed = MarketFeed(broker=_FakeFeedBroker(), bus=bus)
    session = MagicMock()
    session.state = "READY"
    session.bus = bus
    session.market_feed = feed

    app = create_app(session=session)
    client = TestClient(app)

    with client.websocket_connect("/ws/stream") as ws:
        ws.send_text(_json.dumps({
            "type": "subscribe",
            "instruments": ["MCX:CRUDEOIL"],
            "depth": "30",
        }))
        ack = ws.receive_json()
        assert ack["type"] == "error"
        assert "NSE" in ack["message"]
        # The socket survives: a valid NSE depth subscribe still works.
        ws.send_text(_json.dumps({
            "type": "subscribe",
            "instruments": ["NSE:RELIANCE"],
            "depth": "30",
        }))
        ack2 = ws.receive_json()
        assert ack2["type"] == "subscribed"


def test_websocket_subscribe_acks_and_filters():
    """Subscribe ack carries instruments/depth; unsubscribed instruments are filtered."""
    import json as _json
    from decimal import Decimal

    from tradex_domain.instruments import Equity
    from tradex_domain.market import Quote
    from tradex_domain.value_objects import Price

    from tradex_trading.reactive.bus import ReactiveBus

    bus = ReactiveBus()
    session = MagicMock()
    session.state = "READY"
    session.bus = bus

    app = create_app(session=session)
    client = TestClient(app)

    with client.websocket_connect("/ws/stream") as ws:
        ws.send_text(_json.dumps({
            "type": "subscribe",
            "instruments": ["NSE:RELIANCE", "NSE:TCS"],
        }))
        ack = ws.receive_json()
        assert ack["type"] == "subscribed"
        assert ack["instruments"] == ["NSE:RELIANCE", "NSE:TCS"]
        assert ack["depth"] == "off"

        # RELIANCE quote is forwarded; INFY (never subscribed) is filtered.
        bus.publish(Quote(
            instrument=Equity.of("NSE", "RELIANCE"),
            ltp=Price(value=Decimal("1319.2")),
        ))
        bus.publish(Quote(
            instrument=Equity.of("NSE", "INFY"),
            ltp=Price(value=Decimal("1900.0")),
        ))
        msg = ws.receive_json()
        assert msg["type"] == "quote"
        assert msg["instrument"] == "NSE:RELIANCE"
        assert msg["ltp"] == "1319.2"

        ws.send_text(_json.dumps({
            "type": "unsubscribe",
            "instruments": ["NSE:RELIANCE"],
        }))
        ack = ws.receive_json()
        assert ack["type"] == "unsubscribed"
        assert ack["instruments"] == ["NSE:RELIANCE"]

        # After unsubscribe, RELIANCE quotes are no longer forwarded.
        bus.publish(Quote(
            instrument=Equity.of("NSE", "RELIANCE"),
            ltp=Price(value=Decimal("1320.0")),
        ))
        bus.publish(Quote(
            instrument=Equity.of("NSE", "TCS"),
            ltp=Price(value=Decimal("4100.0")),
        ))
        msg = ws.receive_json()
        assert msg["type"] == "quote"
        assert msg["instrument"] == "NSE:TCS"


def test_websocket_subscribe_invalid_instrument_errors():
    """Malformed instrument ids produce an error ack, not a crash."""
    import json as _json

    from tradex_trading.reactive.bus import ReactiveBus

    bus = ReactiveBus()
    session = MagicMock()
    session.state = "READY"
    session.bus = bus

    app = create_app(session=session)
    client = TestClient(app)

    with client.websocket_connect("/ws/stream") as ws:
        ws.send_text(_json.dumps({
            "type": "subscribe",
            "instruments": ["NOT-A-VALID-ID"],
        }))
        msg = ws.receive_json()
        assert msg["type"] == "error"

        ws.send_text(_json.dumps({"type": "bogus"}))
        msg = ws.receive_json()
        assert msg["type"] == "error"


# ---------------------------------------------------------------------------
# WebSocket — send-side backpressure (bounded outbound queue, drop-oldest)
# ---------------------------------------------------------------------------


def test_enqueue_drop_oldest_bounds_queue():
    """Overflow drops the oldest queued message; the newest always lands."""
    import asyncio

    from tradex_trading.interface.fastapi_app import _enqueue_drop_oldest

    async def _run():
        queue: asyncio.Queue = asyncio.Queue(maxsize=3)
        assert _enqueue_drop_oldest(queue, {"seq": 1}) == 0
        assert _enqueue_drop_oldest(queue, {"seq": 2}) == 0
        assert _enqueue_drop_oldest(queue, {"seq": 3}) == 0
        # Queue full: seq 1 is evicted, seq 4 lands, one drop reported.
        assert _enqueue_drop_oldest(queue, {"seq": 4}) == 1
        items = []
        while not queue.empty():
            items.append(queue.get_nowait())
        assert items == [{"seq": 2}, {"seq": 3}, {"seq": 4}]

    asyncio.run(_run())


def test_enqueue_control_drops_oldest_control_on_overflow():
    """A full control queue drops its own oldest control; ticks are untouched."""
    import asyncio

    from tradex_trading.interface.fastapi_app import _enqueue_control_drop_oldest

    async def _run():
        control: asyncio.Queue = asyncio.Queue(maxsize=2)
        ticks: asyncio.Queue = asyncio.Queue(maxsize=4)
        for i in range(4):
            ticks.put_nowait({"seq": i, "kind": "tick"})
        # Control has room — no drop.
        assert _enqueue_control_drop_oldest(control, {"kind": "ack-1"}) == 0
        assert _enqueue_control_drop_oldest(control, {"kind": "ack-2"}) == 0
        # Control is now full: the oldest control (ack-1) is evicted, the
        # newest fill lands, and the tick backlog is never touched.
        assert _enqueue_control_drop_oldest(control, {"kind": "fill"}) == 1
        remaining = []
        while not control.empty():
            remaining.append(control.get_nowait())
        assert remaining == [{"kind": "ack-2"}, {"kind": "fill"}]
        ticks_left = []
        while not ticks.empty():
            ticks_left.append(ticks.get_nowait())
        assert ticks_left == [{"seq": 0, "kind": "tick"}, {"seq": 1, "kind": "tick"},
                              {"seq": 2, "kind": "tick"}, {"seq": 3, "kind": "tick"}]

    asyncio.run(_run())


def test_websocket_stats_surfaces_drop_counter():
    """A stats request reports the per-connection drop counter."""
    import json as _json

    from tradex_trading.reactive.bus import ReactiveBus

    bus = ReactiveBus()
    session = MagicMock()
    session.state = "READY"
    session.bus = bus

    app = create_app(session=session, outbound_max=2)
    client = TestClient(app)

    with client.websocket_connect("/ws/stream") as ws:
        ws.send_text(_json.dumps({"type": "stats"}))
        msg = ws.receive_json()
        assert msg["type"] == "stats"
        assert msg["dropped"] == 0
        assert "queued_ticks" in msg
        assert "queued_control" in msg


def test_websocket_tiny_outbound_queue_still_forwards_quotes():
    """A 2-slot outbound queue keeps quotes flowing (drop-oldest, no crash)."""
    import json as _json
    from decimal import Decimal

    from tradex_domain.instruments import Equity
    from tradex_domain.market import Quote
    from tradex_domain.value_objects import Price

    from tradex_trading.reactive.bus import ReactiveBus

    bus = ReactiveBus()
    session = MagicMock()
    session.state = "READY"
    session.bus = bus

    app = create_app(session=session, outbound_max=2)
    client = TestClient(app)

    with client.websocket_connect("/ws/stream") as ws:
        ws.send_text(_json.dumps({
            "type": "subscribe",
            "instruments": ["NSE:RELIANCE"],
        }))
        ack = ws.receive_json()
        assert ack["type"] == "subscribed"
        # Flood quotes far past the tiny queue — the writer must keep
        # draining without a crash or stall. Drop-oldest guarantees the
        # NEWEST quote (1309) is always delivered, so wait for it.
        for i in range(10):
            bus.publish(Quote(
                instrument=Equity.of("NSE", "RELIANCE"),
                ltp=Price(value=Decimal(f"{1300 + i}.0")),
            ))
        seen = []
        deadline = 0
        while deadline < 50:
            msg = ws.receive_json()
            seen.append(msg)
            deadline += 1
            if msg.get("type") == "quote" and msg.get("ltp") == "1309.0":
                break
        assert any(m["type"] == "quote" for m in seen)  # quotes flowed
        assert seen[-1]["type"] == "quote" and seen[-1]["ltp"] == "1309.0"


# ---------------------------------------------------------------------------
# WebSocket — live feed capabilities, cap enforcement, snapshot
# ---------------------------------------------------------------------------


def _make_feed_session(broker, bus=None, capabilities=None):
    """Build a live-mode TradingSession carrying a real MarketFeed."""
    from tradex_domain import BrokerId

    from tradex_trading.execution.engine import ExecutionEngine
    from tradex_trading.execution.fill_sources import PaperFillSource
    from tradex_trading.execution.trading_cache import TradingCache
    from tradex_trading.reactive.bus import ReactiveBus
    from tradex_trading.runtime.market_feed import MarketFeed
    from tradex_trading.sdk.session import TradingSession

    _bus = bus if bus is not None else ReactiveBus()
    session = TradingSession(
        broker=broker,
        bus=_bus,
        engine=ExecutionEngine(bus=_bus, fill_source=PaperFillSource()),
        cache=TradingCache(),
        broker_id=BrokerId.PAPER,
        mode="live",
    )
    session.start()  # READY — enables MarketService
    session._market_feed = MarketFeed(broker=broker, bus=_bus, capabilities=capabilities)
    return session


def test_websocket_subscribe_acks_feed_capabilities():
    """Subscribe ack advertises the broker's streaming capabilities."""
    import json as _json
    from decimal import Decimal

    from tradex_brokers.common.capabilities import dhan_capabilities
    from tradex_domain.market import Quote
    from tradex_domain.value_objects import Price

    class _CapBroker:
        capabilities = dhan_capabilities()

        def get_quote(self, instrument):
            return Quote(instrument=instrument, ltp=Price(value=Decimal("1")))

        def subscribe_quotes(self, instruments, handler):
            return "q"

        def subscribe_depth(self, instruments, handler):
            return "d"

        def unsubscribe(self, sub):
            pass

    broker = _CapBroker()
    session = _make_feed_session(broker, capabilities=dhan_capabilities())
    app = create_app(session=session)
    client = TestClient(app)

    with client.websocket_connect("/ws/stream") as ws:
        ws.send_text(_json.dumps({
            "type": "subscribe",
            "instruments": ["NSE:RELIANCE"],
        }))
        ack = ws.receive_json()
        assert ack["type"] == "subscribed"
        assert ack["feed"]["depth_levels"] == 20
        assert ack["feed"]["max_stream_instruments"] == 1000
        assert ack["feed"]["dropped"] == 0  # drop counter surfaced in the ack


def test_websocket_subscribe_over_cap_errors_loudly():
    """Subscribing beyond the broker cap yields an error ack, not a silent drop."""
    import json as _json
    from decimal import Decimal

    from tradex_domain.capabilities import BrokerCapabilities
    from tradex_domain.market import Quote
    from tradex_domain.value_objects import Price

    class _CapBroker:
        capabilities = BrokerCapabilities(max_stream_instruments=1)

        def get_quote(self, instrument):
            return Quote(instrument=instrument, ltp=Price(value=Decimal("1")))

        def subscribe_quotes(self, instruments, handler):
            return "q"

        def subscribe_depth(self, instruments, handler):
            return "d"

        def unsubscribe(self, sub):
            pass

    broker = _CapBroker()
    session = _make_feed_session(
        broker, capabilities=BrokerCapabilities(max_stream_instruments=1)
    )
    app = create_app(session=session)
    client = TestClient(app)

    with client.websocket_connect("/ws/stream") as ws:
        ws.send_text(_json.dumps({
            "type": "subscribe",
            "instruments": ["NSE:RELIANCE", "NSE:TCS"],
        }))
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "cap exceeded" in msg["message"]
        # Registry refs rolled back — a second subscribe for one instrument works.
        ws.send_text(_json.dumps({
            "type": "subscribe",
            "instruments": ["NSE:RELIANCE"],
        }))
        ack = ws.receive_json()
        assert ack["type"] == "subscribed"


def test_websocket_subscribe_snapshot_pushes_current_quote():
    """Opt-in snapshot sends a one-shot REST quote right after the ack."""
    import json as _json
    from decimal import Decimal

    from tradex_brokers.common.capabilities import dhan_capabilities
    from tradex_domain.market import Quote
    from tradex_domain.value_objects import Price

    class _QuoteBroker:
        capabilities = dhan_capabilities()

        def get_quote(self, instrument):
            return Quote(
                instrument=instrument,
                ltp=Price(value=Decimal("1319.20")),
                provider="test",
            )

        def subscribe_quotes(self, instruments, handler):
            return "q"

        def subscribe_depth(self, instruments, handler):
            return "d"

        def unsubscribe(self, sub):
            pass

    broker = _QuoteBroker()
    session = _make_feed_session(broker, capabilities=dhan_capabilities())
    app = create_app(session=session)
    client = TestClient(app)

    with client.websocket_connect("/ws/stream") as ws:
        ws.send_text(_json.dumps({
            "type": "subscribe",
            "instruments": ["NSE:RELIANCE"],
            "snapshot": True,
        }))
        ack = ws.receive_json()
        assert ack["type"] == "subscribed"
        msg = ws.receive_json()
        assert msg["type"] == "quote"
        assert msg["instrument"] == "NSE:RELIANCE"
        assert msg["ltp"] == "1319.20"


# ---------------------------------------------------------------------------
# WebSocket — broker order-stream bridge (subscribe_orders → control queue)
# ---------------------------------------------------------------------------


def _make_order_stream_session(backend=None):
    """Live-mode session wired with a fake broker order-stream backend."""
    from tradex_domain import BrokerId

    from tradex_trading.execution.engine import ExecutionEngine
    from tradex_trading.execution.fill_sources import PaperFillSource
    from tradex_trading.execution.trading_cache import TradingCache
    from tradex_trading.reactive.bus import ReactiveBus
    from tradex_trading.sdk.session import TradingSession

    _bus = ReactiveBus()
    session = TradingSession(
        broker=MagicMock(),
        bus=_bus,
        engine=ExecutionEngine(bus=_bus, fill_source=PaperFillSource()),
        cache=TradingCache(),
        broker_id=BrokerId.PAPER,
        mode="live",
        stream_backend=backend,
    )
    session.start()
    session._market_feed = None
    return session, _bus


def test_websocket_subscribe_orders_forwards_broker_order_events():
    """A broker order event pushed through the backend handler reaches the client
    as a control-priority ``order`` message."""
    import json as _json
    from decimal import Decimal

    from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
    from tradex_domain.execution import Order
    from tradex_domain.instruments import Equity
    from tradex_domain.value_objects import Price, Quantity

    class FakeBackend:
        def __init__(self):
            self.handler = None
            self.unsubscribed = []

        def subscribe_orders(self, handler):
            self.handler = handler
            return "order-1"

        def unsubscribe(self, subscription):
            self.unsubscribed.append(subscription)

    backend = FakeBackend()
    session, _bus = _make_order_stream_session(backend)
    app = create_app(session=session)
    client = TestClient(app)

    with client.websocket_connect("/ws/stream") as ws:
        ws.send_text(_json.dumps({"type": "subscribe_orders"}))
        ack = ws.receive_json()
        assert ack["type"] == "subscribed_orders"
        assert backend.handler is not None

        order = Order(
            order_id="ORD-999",
            instrument=Equity.of("NSE", "RELIANCE"),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("2500")),
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.ACK,
        )
        backend.handler(order)
        msg = ws.receive_json()
        assert msg["type"] == "order"
        assert msg["order_id"] == "ORD-999"
        assert msg["instrument"] == "NSE:RELIANCE"
        assert msg["status"] == "ACK"
        assert msg["side"] == "BUY"
        assert msg["quantity"] == "10"
        assert msg["price"] == "2500"

        # Unsubscribe cancels the backend handle.
        ws.send_text(_json.dumps({"type": "unsubscribe_orders"}))
        ack = ws.receive_json()
        assert ack["type"] == "unsubscribed_orders"
        assert backend.unsubscribed == ["order-1"]


def test_websocket_subscribe_orders_no_backend_falls_back_to_bus():
    """Without a wired backend, subscribe_orders rides the bus OrderPlaced stream."""
    import json as _json
    from decimal import Decimal

    from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
    from tradex_domain.events import OrderPlaced
    from tradex_domain.execution import Order
    from tradex_domain.instruments import Equity
    from tradex_domain.value_objects import Price, Quantity

    session, _bus = _make_order_stream_session(backend=None)
    app = create_app(session=session)
    client = TestClient(app)

    with client.websocket_connect("/ws/stream") as ws:
        ws.send_text(_json.dumps({"type": "subscribe_orders"}))
        ack = ws.receive_json()
        assert ack["type"] == "subscribed_orders"

        order = Order(
            order_id="ORD-777",
            instrument=Equity.of("NSE", "RELIANCE"),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("5")),
            price=Price(value=Decimal("2500")),
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.ACK,
        )
        _bus.publish(OrderPlaced(order=order))
        msg = ws.receive_json()
        assert msg["type"] == "order"
        assert msg["order_id"] == "ORD-777"


# ---------------------------------------------------------------------------
# /health/ready — true readiness
# ---------------------------------------------------------------------------


def test_health_ready_reports_session_state():
    """A READY session is reported as ready with its state."""
    session = MagicMock()
    session.state = "READY"
    app = create_app(session=session)
    client = TestClient(app)
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["session_state"] == "READY"


def test_health_ready_503_when_session_not_ready():
    """A session that is not READY fails readiness with 503."""
    session = MagicMock()
    session.state = "NEW"
    app = create_app(session=session)
    client = TestClient(app)
    r = client.get("/health/ready")
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# start_fastapi_server — pre-bind readiness probe, workers/reload
# ---------------------------------------------------------------------------


class TestStartFastapiServer:
    """start_fastapi_server — pre-bind readiness probe + workers/reload."""

    @pytest.fixture(autouse=True)
    def _uvicorn_and_clean_env(self, monkeypatch):
        pytest.importorskip("uvicorn")
        monkeypatch.delenv("TRADEX_SERVE_BROKER", raising=False)
        monkeypatch.delenv("TRADEX_SERVE_API_KEY", raising=False)

    @staticmethod
    def _ready_session():
        session = MagicMock()
        session.state = "READY"
        session.broker_id = "PAPER"
        return session

    def test_rejects_unready_session_before_binding(self):
        """A NEW session fails loudly before uvicorn binds a socket."""
        session = MagicMock()
        session.state = "NEW"
        with patch("uvicorn.run") as uvicorn_run:
            with pytest.raises(ValueError, match="not ready"):
                start_fastapi_server(session, port=8081)
        uvicorn_run.assert_not_called()

    def test_ready_session_binds_object_app(self):
        """A READY session serves the object app directly (single process)."""
        session = self._ready_session()
        with patch("uvicorn.run") as uvicorn_run:
            start_fastapi_server(session, port=8081)
        uvicorn_run.assert_called_once()
        app = uvicorn_run.call_args.args[0]
        assert isinstance(app, FastAPI)

    def test_rejects_zero_workers(self):
        session = self._ready_session()
        with patch("uvicorn.run") as uvicorn_run:
            with pytest.raises(ValueError, match="workers must be >= 1"):
                start_fastapi_server(session, workers=0)
        uvicorn_run.assert_not_called()

    def test_workers_uses_importable_factory(self):
        """workers > 1 delegates to serve_app via an import string (spawn-safe)."""
        import os

        session = self._ready_session()
        with patch("uvicorn.run") as uvicorn_run:
            start_fastapi_server(session, workers=2)
        uvicorn_run.assert_called_once()
        app_spec = uvicorn_run.call_args.args[0]
        assert app_spec == "tradex_trading.interface.fastapi_app:serve_app"
        assert uvicorn_run.call_args.kwargs["factory"] is True
        assert uvicorn_run.call_args.kwargs["workers"] == 2
        assert os.environ.get("TRADEX_SERVE_BROKER") == "PAPER"

    def test_reload_uses_importable_factory(self):
        """--reload delegates to serve_app so the reloaded process rebuilds a session."""
        session = self._ready_session()
        with patch("uvicorn.run") as uvicorn_run:
            start_fastapi_server(session, reload=True)
        uvicorn_run.assert_called_once()
        assert uvicorn_run.call_args.args[0] == "tradex_trading.interface.fastapi_app:serve_app"
        assert uvicorn_run.call_args.kwargs["factory"] is True
        assert uvicorn_run.call_args.kwargs["reload"] is True

    def test_factory_forwards_api_key_in_env(self):
        """The API key travels in the environment for spawned workers."""
        import os

        session = self._ready_session()
        with patch("uvicorn.run") as uvicorn_run:
            start_fastapi_server(session, workers=2, api_key="secret")
        assert os.environ.get("TRADEX_SERVE_API_KEY") == "secret"
        assert uvicorn_run.call_args.kwargs["factory"] is True


# ---------------------------------------------------------------------------
# End-to-end: real paper TradingSession through the served API
# ---------------------------------------------------------------------------


class TestPaperSessionEndToEnd:
    """Boot a real paper TradingSession and serve it — live state via TestClient.

    Unlike the mocked-session tests above, every assertion flows through the
    actual paper execution engine: a submitted order is filled, lands in the
    session's shared OMS cache, and shows up in /orders and /positions. This
    is the smoke that the session wiring (engine cache == session cache) and
    the route read paths agree.
    """

    @pytest.fixture()
    def client(self):
        from tradex_trading.sdk.session import TradingSession

        session = TradingSession.paper()
        app = create_app(session=session)
        yield TestClient(app)
        session.stop()

    def test_health_ready_reports_real_readiness(self, client):
        r = client.get("/health/ready")
        assert r.status_code == 200
        assert r.json()["session_state"] == "READY"

    def test_place_order_then_list_and_fetch(self, client):
        r = client.post(
            "/orders",
            json={
                "exchange": "NSE",
                "symbol": "RELIANCE",
                "side": "BUY",
                "quantity": 10,
                "order_type": "MARKET",
            },
        )
        assert r.status_code == 200
        order_id = r.json()["order_id"]
        assert order_id

        # The filled order is visible in the session's order book.
        r = client.get("/orders")
        assert r.status_code == 200
        ids = [o["order_id"] for o in r.json()]
        assert order_id in ids

        # And fetchable by id (raw string id from the URL path).
        r = client.get(f"/orders/{order_id}")
        assert r.status_code == 200
        assert r.json()["order_id"] == order_id

    def test_filled_limit_order_projects_position(self, client):
        r = client.post(
            "/orders",
            json={
                "exchange": "NSE",
                "symbol": "TCS",
                "side": "BUY",
                "quantity": 5,
                "order_type": "LIMIT",
                "price": 100,
            },
        )
        assert r.status_code == 200

        r = client.get("/positions")
        assert r.status_code == 200
        positions = r.json()
        matches = [p for p in positions if "TCS" in p["instrument"]]
        assert matches, positions
        pos = matches[0]
        assert pos["quantity"] == "5"
        assert pos["avg_price"] == "100"
        assert pos["is_long"] is True

    def test_account_returns_paper_balance(self, client):
        r = client.get("/account")
        assert r.status_code == 200
        assert r.json()["balance"] is not None

    def test_history_endpoint_returns_bars(self, client):
        """GET /history works end-to-end (2-arg market.history call → default
        lookback) instead of 500ing on the missing window."""
        r = client.get("/history/NSE:RELIANCE?timeframe=1d")
        assert r.status_code == 200
        assert r.json() == []

    def test_option_chain_is_capability_loud_on_paper(self, client):
        """Paper declares no option-chain support — the API reflects the real
        session capability surface (500 + capability message) rather than
        fabricating chain data."""
        r = client.get("/option-chain/NSE:RELIANCE")
        assert r.status_code == 500
        assert "supports_option_chain" in r.json()["detail"]


# ---------------------------------------------------------------------------
# start_fastapi_server — live-broker guards and env hygiene
# ---------------------------------------------------------------------------


class TestStartFastapiServerGuards:
    """Live-broker multi-process guard + serve-spec env hygiene."""

    @pytest.fixture(autouse=True)
    def _uvicorn_and_clean_env(self, monkeypatch):
        pytest.importorskip("uvicorn")
        monkeypatch.delenv("TRADEX_SERVE_BROKER", raising=False)
        monkeypatch.delenv("TRADEX_SERVE_API_KEY", raising=False)

    @staticmethod
    def _ready_session():
        session = MagicMock()
        session.state = "READY"
        session.broker_id = "PAPER"
        return session

    def test_rejects_workers_for_live_broker(self):
        """Live brokers must not multi-process: each worker would re-authenticate."""
        session = self._ready_session()
        session.broker_id = "UPSTOX"
        with patch("uvicorn.run") as uvicorn_run:
            with pytest.raises(ValueError, match="live brokers re-authenticate"):
                start_fastapi_server(session, workers=2)
            with pytest.raises(ValueError, match="live brokers re-authenticate"):
                start_fastapi_server(session, reload=True)
        uvicorn_run.assert_not_called()

    def test_object_path_leaves_no_stale_spec(self, monkeypatch):
        """Single-process serve must not leak the serve spec into the env."""
        monkeypatch.setenv("TRADEX_SERVE_BROKER", "STALE")
        monkeypatch.setenv("TRADEX_SERVE_API_KEY", "stale-key")
        session = self._ready_session()
        with patch("uvicorn.run"):
            start_fastapi_server(session, port=8081)
        import os

        assert os.environ.get("TRADEX_SERVE_BROKER") is None
        assert os.environ.get("TRADEX_SERVE_API_KEY") is None

    def test_serve_app_factory_builds_ready_paper_session(self, monkeypatch):
        """serve_app() — the code that runs in each spawned worker — builds a
        READY paper session app and forwards the env API key."""
        from tradex_trading.interface.fastapi_app import serve_app

        monkeypatch.setenv("TRADEX_SERVE_BROKER", "PAPER")
        monkeypatch.setenv("TRADEX_SERVE_API_KEY", "worker-secret")
        app = serve_app()
        assert isinstance(app, FastAPI)
        assert str(app.state.session.state) == "READY"
        assert app.state.api_key == "worker-secret"
