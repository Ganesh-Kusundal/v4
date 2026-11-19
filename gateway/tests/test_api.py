from __future__ import annotations

import threading
import time
from datetime import date, timedelta

from fastapi.testclient import TestClient
from gateway_support import FakeBroker, gateway_config, make_candle
from tradex_domain.errors import CapabilityNotSupportedError
from tradex_domain.instruments import Future, Option
from tradex_domain.market import HistoricalSeries
from tradex_gateway.app import create_app
from tradex_gateway.brokers import BrokerRuntime


def _receive_op(ws, op: str, limit: int = 10) -> dict:
    for _ in range(limit):
        frame = ws.receive_json()
        if frame.get("op") == op:
            return frame
    raise AssertionError(f"no {op!r} frame within {limit} frames")


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


def test_naive_history_timestamps_are_rejected():
    app, _, _ = _app()
    with TestClient(app) as client:
        response = client.get(
            "/v1/history/IDX:NIFTY",
            params={"from": "2026-11-06T09:15:00", "to": "2026-11-06T09:16:00"},
        )
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "invalid_request"
        assert "UTC offset" in error["message"]


def test_naive_batch_history_timestamps_are_per_item_errors():
    app, _, _ = _app()
    with TestClient(app) as client:
        response = client.post(
            "/v1/history/batch",
            json={
                "requests": [
                    {
                        "instrument_id": "IDX:NIFTY",
                        "from": "2026-11-06T09:15:00",
                        "to": "2026-11-06T09:16:00",
                    }
                ]
            },
        )
        assert response.status_code == 200
        item = response.json()[0]
        assert item["instrument_id"] == "IDX:NIFTY"
        assert item["error"]["code"] == "invalid_request"
        assert "UTC offset" in item["error"]["message"]


class _RecordingExecutor:
    """Delegates to the real pool while recording every submitted callable."""

    def __init__(self, inner):
        self._inner = inner
        self.submitted: list[object] = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append(fn)
        return self._inner.submit(fn, *args, **kwargs)

    def shutdown(self, *args, **kwargs):
        return self._inner.shutdown(*args, **kwargs)


def test_batch_route_fans_out_on_the_app_batch_executor():
    app, dhan, upstox = _app()
    worker_names: list[str] = []

    def history(instrument, tf, start, end):
        worker_names.append(threading.current_thread().name)
        return HistoricalSeries(
            instrument=instrument,
            timeframe=tf,
            candles=[make_candle(instrument, end)],
            start=start,
            end=end,
        )

    dhan.history_impl = upstox.history_impl = history
    with TestClient(app) as client:
        recorder = _RecordingExecutor(app.state.executor)
        app.state.executor = recorder
        response = client.post(
            "/v1/history/batch",
            json={
                "requests": [
                    {
                        "instrument_id": "IDX:NIFTY",
                        "from": "2026-11-06T09:15:00Z",
                        "to": "2026-11-06T09:16:00Z",
                    },
                    {
                        "instrument_id": "IDX:NIFTY",
                        "from": "2026-11-06T09:17:00Z",
                        "to": "2026-11-06T09:18:00Z",
                    },
                ]
            },
        )
        assert response.status_code == 200, response.text
        results = response.json()
        assert len(results) == 2
        assert all(item["bars"] for item in results)
        # The route submits batch() once (one request-pool task per HTTP call)
        # and every item runs on a worker of the dedicated batch pool, whose
        # threads carry the "gateway-batch" prefix. A per-request nested pool
        # would have run them on "ThreadPoolExecutor-…" threads and the request
        # pool's own "gateway-broker" threads are the ones that must stay free,
        # because batch() itself occupies one of them while it waits.
        assert len(recorder.submitted) == 1
        assert len(worker_names) == 2
        assert all(name.startswith("gateway-batch") for name in worker_names)


def test_auth_required_when_configured():
    app, _, _ = _app(api_key="secret", allow_anon=False)
    with TestClient(app) as client:
        denied = client.get("/v1/quote/IDX:NIFTY")
        assert denied.status_code == 401
        allowed = client.get("/v1/quote/IDX:NIFTY", headers={"X-API-Key": "secret"})
        assert allowed.status_code == 200


def test_metrics_requires_api_key_when_configured():
    app, _, _ = _app(api_key="secret", allow_anon=False)
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", headers={"X-API-Key": "secret"}).status_code == 200


def test_metrics_stays_open_when_anonymous_allowed():
    app, _, _ = _app(api_key=None, allow_anon=True)
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 200


def test_metrics_scrape_does_not_consume_consumer_quota():
    app, _, _ = _app(api_key="secret", allow_anon=False, rest_rate_per_second=0.001, rest_burst=1)
    with TestClient(app) as client:
        for _ in range(5):
            assert client.get("/metrics", headers={"X-API-Key": "secret"}).status_code == 200
        assert client.get("/v1/quote/IDX:NIFTY", headers={"X-API-Key": "secret"}).status_code == 200
        limited = client.get("/v1/quote/IDX:NIFTY", headers={"X-API-Key": "secret"})
        assert limited.status_code == 429


def test_500_does_not_leak_exception_text():
    app, _, _ = _app()
    # Starlette re-raises after the 500 handler writes its response; keep the
    # client from re-raising so the handler-produced body can be inspected.
    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.history.history = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("secret-internal-detail")
        )
        response = client.get(
            "/v1/history/IDX:NIFTY",
            params={"from": "2026-11-06T09:15:00Z", "to": "2026-11-06T09:16:00Z"},
        )
        assert response.status_code == 500
        assert "secret-internal-detail" not in response.text
        assert response.json()["error"]["code"] == "internal_error"
        assert response.json()["error"]["message"] == "internal server error"


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


def test_websocket_rejects_invalid_payload_and_stays_connected():
    app, _, _ = _app()
    with TestClient(app) as client:
        with client.websocket_connect("/v1/stream") as ws:
            ws.receive_json()  # initial status
            ws.send_text("not-json")
            error = ws.receive_json()
            assert error["op"] == "error"
            assert error["code"] == "invalid_request"

            ws.send_json(
                {
                    "op": "subscribe",
                    "instruments": ["NSE:RELIANCE"],
                    "mode": "quote",
                    "levels": "bad",
                }
            )
            error = ws.receive_json()
            assert error["op"] == "error"
            assert error["code"] == "invalid_request"

            ws.send_json({"op": "ping"})
            assert _receive_op(ws, "pong")["op"] == "pong"

            ws.send_bytes(b"x")
            error = ws.receive_json()
            assert error["op"] == "error"
            assert error["code"] == "invalid_request"
            ws.send_json({"op": "ping"})
            assert _receive_op(ws, "pong")["op"] == "pong"

            # An integer literal past CPython's 4300-digit limit makes json.loads
            # raise a plain ValueError rather than JSONDecodeError.
            ws.send_text("9" * 4301)
            error = ws.receive_json()
            assert error["op"] == "error"
            assert error["code"] == "invalid_request"
            ws.send_json({"op": "ping"})
            assert _receive_op(ws, "pong")["op"] == "pong"


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


INTERNAL_ID_PATTERNS = [r"NSE_EQ\|", r"NSE_OPT\|", r"\bINE\d{5}[A-Z]{2}\d{4}\b", r"provider key"]


def test_no_response_contains_internal_ids():
    import re

    app, _, _ = _app()
    urls = [
        "/v1/quote/NSE:RELIANCE",
        "/v1/depth/NSE:RELIANCE",
        "/v1/history/NSE:RELIANCE?timeframe=1m&from=2026-11-09T09:15:00%2B05:30&to=2026-11-09T09:20:00%2B05:30",
        "/v1/option-chain/NSE:NIFTY",
        "/v1/futures/NSE:NIFTY",
        "/v1/instruments/search?q=REL",
        "/v1/instruments/resolve?q=NSE:RELIANCE",
        "/v1/health",
        "/v1/brokers",
    ]
    with TestClient(app) as client:
        for url in urls:
            response = client.get(url)
            body = response.text
            for pattern in INTERNAL_ID_PATTERNS:
                assert not re.search(pattern, body), f"{pattern} leaked in {url}"


def test_market_data_surface_parity():
    """Every market-data broker method has a gateway route."""
    from tradex_brokers.common.base import BaseBroker
    from tradex_brokers.dhan.adapter import DhanBroker
    from tradex_brokers.upstox.adapter import UpstoxBroker

    app, _, _ = _app()
    paths = set()
    for route in app.routes:
        if getattr(route, "path", None):
            paths.add(route.path)
        original = getattr(route, "original_router", None)
        if original is not None:
            paths.update(getattr(r, "path", None) for r in original.routes)
    paths.discard(None)
    # get_option_chain is declared on the concrete adapters (not BaseBroker).
    expected = {
        (BaseBroker, "get_quote"): "/v1/quote/{instrument_id}",
        (BaseBroker, "quote_batch"): "/v1/quotes",
        (BaseBroker, "depth"): "/v1/depth/{instrument_id}",
        (BaseBroker, "history"): "/v1/history/{instrument_id}",
        (BaseBroker, "search"): "/v1/instruments/search",
        (DhanBroker, "get_option_chain"): "/v1/option-chain/{underlying}",
        (UpstoxBroker, "get_option_chain"): "/v1/option-chain/{underlying}",
        (BaseBroker, "future_chain"): "/v1/futures/{underlying}",
    }
    for (cls, method), route in expected.items():
        assert hasattr(cls, method), f"{cls.__name__}.{method}"
        assert route in paths, route


def test_slow_broker_call_returns_timeout_envelope():
    app, _, upstox = _app(request_timeout=0.2)
    upstox.get_quote = lambda instrument: time.sleep(2)
    with TestClient(app) as client:
        started = time.monotonic()
        response = client.get("/v1/quote/IDX:NIFTY")
        assert time.monotonic() - started < 1.5
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["retryable"] is True
    assert error["message"] == "request timed out"


def test_slow_boot_is_not_bounded_by_request_timeout(monkeypatch):
    import tradex_gateway.app as app_module

    def slow_build(cfg):
        time.sleep(0.5)
        return {"dhan": BrokerRuntime("dhan", FakeBroker("dhan"))}

    monkeypatch.setattr(app_module, "build_runtimes", slow_build)
    monkeypatch.setattr(app_module, "load_local_env", lambda: ())
    app = create_app(gateway_config(request_timeout=0.1, brokers=("dhan",)))
    with TestClient(app) as client:
        assert client.get("/v1/health").status_code == 200


def test_slow_batch_item_gets_timeout_envelope_while_fast_item_returns():
    app, dhan, upstox = _app(request_timeout=1.0)

    def history(instrument, tf, start, end):
        if str(tf.value) != "1m":
            time.sleep(2)
        return HistoricalSeries(
            instrument=instrument,
            timeframe=tf,
            candles=[make_candle(instrument, end)],
            start=start,
            end=end,
        )

    dhan.history_impl = upstox.history_impl = history
    window = {"from": "2026-11-06T09:15:00Z", "to": "2026-11-06T09:16:00Z"}
    with TestClient(app) as client:
        response = client.post(
            "/v1/history/batch",
            json={
                "requests": [
                    {"instrument_id": "IDX:NIFTY", "timeframe": "1m", **window},
                    {"instrument_id": "IDX:NIFTY", "timeframe": "5m", **window},
                ]
            },
        )
    assert response.status_code == 200, response.text
    fast, slow = response.json()
    assert fast["bars"]
    assert slow["error"]["message"] == "request timed out"
    assert slow["error"]["retryable"] is True


def test_quotes_bad_id_is_per_item_error():
    app, _, _ = _app()
    with TestClient(app) as client:
        response = client.post("/v1/quotes", json={"instruments": ["IDX:NIFTY", "bogus"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert "IDX:NIFTY" in body["quotes"]
    assert body["errors"]["bogus"]["code"] == "invalid_request"


def test_quotes_fallback_is_capped():
    app, dhan, upstox = _app(quote_fallback_cap=25)

    def unsupported(instruments):
        raise CapabilityNotSupportedError("quote_batch")

    dhan.quote_batch = upstox.quote_batch = unsupported
    ids = [f"NSE:SYM{i}" for i in range(60)]
    with TestClient(app) as client:
        response = client.post("/v1/quotes", json={"instruments": ids})
    assert response.status_code == 200, response.text
    body = response.json()
    calls = [c for b in (dhan, upstox) for c in b.calls if c[0] == "get_quote"]
    assert len(calls) == 25
    assert len(body["quotes"]) == 25
    assert [e["message"] for e in body["errors"].values()] == [
        "quote_batch unsupported; fallback capped"
    ] * 35


def test_health_detail_requires_api_key_but_liveness_does_not():
    app, _, _ = _app(api_key="secret", allow_anon=False)
    with TestClient(app) as client:
        anon = client.get("/v1/health")
        assert anon.status_code == 200
        assert anon.json() == {"status": "ok", "ready": True}
        keyed = client.get("/v1/health", headers={"X-API-Key": "secret"}).json()
        assert keyed["ready"] is True
        assert [row["name"] for row in keyed["brokers"]] == ["dhan", "upstox"]
        assert "feed" in keyed
