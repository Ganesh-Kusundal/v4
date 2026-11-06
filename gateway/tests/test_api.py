from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient
from gateway_support import FakeBroker, gateway_config, make_candle
from tradex_domain.instruments import Future, Option
from tradex_domain.market import HistoricalSeries
from tradex_gateway.app import create_app
from tradex_gateway.brokers import BrokerRuntime


def _app(**overrides):
    dhan, upstox = FakeBroker("dhan"), FakeBroker("upstox")

    def history(instrument, tf, start, end):
        return HistoricalSeries(
            instrument=instrument,
            timeframe=tf,
            candles=[make_candle(instrument, end)],
            start=start,
            end=end,
        )

    upstox.history_impl = history
    dhan.history_impl = history
    app = create_app(
        gateway_config(**overrides),
        {
            "dhan": BrokerRuntime("dhan", dhan),
            "upstox": BrokerRuntime("upstox", upstox),
        },
    )
    return app, dhan, upstox


def test_health_and_broker_surfaces():
    app, _, _ = _app()
    with TestClient(app) as client:
        health = client.get("/v1/health")
        assert health.status_code == 200
        assert health.json()["ready"] is True
        brokers = client.get("/v1/brokers")
        assert brokers.status_code == 200
        names = [row["name"] for row in brokers.json()["brokers"]]
        assert names == ["dhan", "upstox"]


def test_history_quote_depth_chain_and_futures():
    app, _, _ = _app()
    with TestClient(app) as client:
        history = client.get(
            "/v1/history/IDX:NIFTY",
            params={
                "timeframe": "1m",
                "from": "2026-11-06T09:15:00Z",
                "to": "2026-11-06T09:16:00Z",
            },
        )
        assert history.status_code == 200, history.text
        assert history.json()["source"] == "broker"
        assert history.json()["broker"] == "upstox"

        quote = client.get("/v1/quote/IDX:NIFTY")
        assert quote.status_code == 200
        assert quote.json()["instrument"] == "IDX:NIFTY"

        quotes = client.post("/v1/quotes", json={"instruments": ["IDX:NIFTY"]})
        assert quotes.status_code == 200
        assert "IDX:NIFTY" in quotes.json()["quotes"]

        depth = client.get("/v1/depth/IDX:NIFTY", params={"levels": 5})
        assert depth.status_code == 200
        assert depth.json()["delivered_levels"] == 5

        chain = client.get("/v1/option-chain/IDX:NIFTY")
        assert chain.status_code == 200, chain.text
        assert chain.json()["strikes"]

        futures = client.get("/v1/futures/IDX:NIFTY")
        assert futures.status_code == 200
        assert futures.json()["futures"] == []


def test_auth_required_when_configured():
    app, _, _ = _app(api_key="secret", allow_anon=False)
    with TestClient(app) as client:
        denied = client.get("/v1/quote/IDX:NIFTY")
        assert denied.status_code == 401
        allowed = client.get("/v1/quote/IDX:NIFTY", headers={"X-API-Key": "secret"})
        assert allowed.status_code == 200


def test_consumer_rate_limit_returns_429():
    app, _, _ = _app(rest_rate_per_second=0.001, rest_burst=1)
    with TestClient(app) as client:
        assert client.get("/v1/quote/IDX:NIFTY").status_code == 200
        limited = client.get("/v1/quote/IDX:NIFTY")
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "consumer_rate_limited"


def test_websocket_subscribe_and_ping():
    app, dhan, _ = _app()
    with TestClient(app) as client:
        with client.websocket_connect("/v1/stream") as ws:
            assert ws.receive_json()["op"] == "status"
            ws.send_json(
                {
                    "op": "subscribe",
                    "instruments": ["NSE:RELIANCE"],
                    "mode": "quote",
                    "levels": 5,
                }
            )
            ack = None
            for _ in range(8):
                frame = ws.receive_json()
                if frame.get("op") == "ack":
                    ack = frame
                    break
            assert ack is not None
            assert ack["broker"] == "dhan"
            assert dhan.quote_sub_calls == 1

            ws.send_json({"op": "ping"})
            pong = None
            for _ in range(8):
                frame = ws.receive_json()
                if frame.get("op") == "pong":
                    pong = frame
                    break
            assert pong is not None


def test_search_resolve_chain_and_futures_include_lot_size():
    app, dhan, _ = _app()
    expiry = date.today() + timedelta(days=7)
    future = Future.of("NFO", "NIFTY", date.today() + timedelta(days=17))
    ce = Option.of("NFO", "NIFTY", expiry, 25000, "CE")
    pe = Option.of("NFO", "NIFTY", expiry, 25000, "PE")
    dhan.futures = [future]
    dhan.registry.lot_sizes = {
        "NSE:RELIANCE": 250,
        str(ce.instrument_id): 75,
        str(pe.instrument_id): 75,
        str(future.instrument_id): 50,
    }
    with TestClient(app) as client:
        search = client.get("/v1/instruments/search", params={"q": "REL"})
        row = next(item for item in search.json() if item["instrument_id"] == "NSE:RELIANCE")
        assert row["lot_size"] == 250
        assert row["lot_size_by_broker"] == {"dhan": 250}

        resolve = client.get("/v1/instruments/resolve", params={"q": "NSE:RELIANCE"})
        assert resolve.json()["lot_size"] == 250

        chain = client.get("/v1/option-chain/IDX:NIFTY")
        assert chain.json()["strikes"][0]["ce"]["lot_size"] == 75

        futures = client.get("/v1/futures/IDX:NIFTY")
        assert futures.json()["futures"][0]["lot_size"] == 50
