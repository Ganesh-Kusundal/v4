"""CI-safe randomized load and fault-injection harness for the gateway.

Everything here runs in-process against fake brokers: no network, no live
credentials, no parquet. The harness is importable for pytest and runnable as a
CLI for larger loads:

    python gateway/tests/load_harness.py --requests 2000 --concurrency 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from gateway_support import (
    FakeRegistry,
    equity,
    gateway_config,
    make_candle,
    make_chain,
    make_depth,
    make_quote,
)
from gateway_support import (
    index as make_index,
)
from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.enums import AssetClass, Timeframe
from tradex_domain.errors import (
    CapabilityNotSupportedError,
    InstrumentNotFoundError,
)
from tradex_domain.instruments import Future, Option
from tradex_domain.market import HistoricalSeries
from tradex_gateway.app import create_app
from tradex_gateway.brokers import BrokerRuntime

_KNOWN_ERROR_CODES = {
    "unauthorized",
    "forbidden",
    "instrument_not_found",
    "invalid_request",
    "unsupported_timeframe",
    "capability_not_supported",
    "consumer_rate_limited",
    "broker_rate_limited",
    "broker_unavailable",
    "no_healthy_broker",
    "feed_unavailable",
    "internal_error",
}


# ---------------------------------------------------------------------------
# Chaos broker
# ---------------------------------------------------------------------------


class ChaosBroker:
    """A fake broker that injects deterministic, seeded soft failures.

    The failures are ``ValueError`` / ``InstrumentNotFoundError`` /
    ``CapabilityNotSupportedError`` so the router falls through to the other
    broker without tripping a 15-60 s cooldown. Cooldown behaviour is covered
    deterministically in ``gateway/tests/test_router.py``.
    """

    def __init__(
        self,
        name: str,
        instruments: list[Any],
        *,
        fail_rate: float = 0.0,
        seed: int = 0,
        depth_levels: int | None = None,
    ) -> None:
        self.name = name
        self.registry = FakeRegistry()
        self.capabilities = BrokerCapabilities(
            supports_batch_market_data=True,
            supports_option_chain=True,
            supports_future_chain=True,
            depth_levels=(
                depth_levels if depth_levels is not None else (20 if name == "dhan" else 30)
            ),
            max_stream_instruments=1000 if name == "dhan" else 500,
            supported_asset_classes=(
                AssetClass.EQUITY,
                AssetClass.INDEX,
                AssetClass.FUTURE,
                AssetClass.OPTION,
            ),
        )
        self._instruments = list(instruments)
        self.registry.known = {str(item.instrument_id) for item in self._instruments}
        self._fail_rate = max(0.0, min(1.0, fail_rate))
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self.calls = 0
        self.failures = 0
        self.call_counts: Counter[str] = Counter()
        self.failure_kinds: Counter[str] = Counter()
        self.subscribe_quotes_calls = 0
        self.subscribe_depth_calls = 0
        self.subscribed: list[str] = []
        self.unsubscribed_instruments: list[str] = []
        self.quote_handlers: list[Any] = []
        self.depth_handlers: list[Any] = []

    # -- chaos ------------------------------------------------------------
    def _maybe_fail(self, method: str) -> None:
        with self._lock:
            self.calls += 1
            self.call_counts[method] += 1
            if self._rng.random() >= self._fail_rate:
                return
            kind = self._rng.choice(("value", "not_found", "capability"))
            self.failures += 1
            self.failure_kinds[kind] += 1
        if kind == "value":
            raise ValueError(f"chaos {method}")
        if kind == "not_found":
            raise InstrumentNotFoundError(f"chaos {method}")
        raise CapabilityNotSupportedError(f"chaos {method}")

    # -- instrument surface ----------------------------------------------
    def _universe(self) -> list[Any]:
        return list(self._instruments)

    def search(self, query: str) -> list[Any]:
        needle = query.strip().upper()
        return [item for item in self._instruments if needle in item.symbol.upper()]

    # -- market data ------------------------------------------------------
    def history(self, instrument, timeframe=None, start=None, end=None, **kwargs):
        self._maybe_fail("history")
        moment = end or datetime.now(UTC)
        return HistoricalSeries(
            instrument=instrument,
            timeframe=timeframe if timeframe is not None else Timeframe.M1,
            candles=[make_candle(instrument, moment)],
            start=start or moment,
            end=end or moment,
        )

    def get_quote(self, instrument):
        self._maybe_fail("quote")
        return make_quote(instrument)

    def quote_batch(self, instruments):
        self._maybe_fail("quote_batch")
        return {item.instrument_id: make_quote(item) for item in instruments}

    def depth(self, instrument):
        self._maybe_fail("depth")
        return make_depth(instrument)

    def get_option_chain(self, underlying, expiry=None):
        self._maybe_fail("chain")
        return make_chain(underlying, expiry)

    def future_chain(self, underlying):
        self._maybe_fail("futures")
        root = underlying.instrument_id.underlying
        return [
            item
            for item in self._instruments
            if isinstance(item, Future) and item.instrument_id.underlying == root
        ]

    # -- streams ----------------------------------------------------------
    def subscribe_quotes(self, instruments, handler):
        with self._lock:
            self.subscribe_quotes_calls += 1
            self.quote_handlers.append(handler)
            self.subscribed.extend(str(item.instrument_id) for item in instruments)
        return f"{self.name}-quotes-{self.subscribe_quotes_calls}"

    def subscribe_depth(self, instruments, handler):
        with self._lock:
            self.subscribe_depth_calls += 1
            self.depth_handlers.append(handler)
            self.subscribed.extend(str(item.instrument_id) for item in instruments)
        return f"{self.name}-depth-{self.subscribe_depth_calls}"

    def subscribe_depth_30(self, instruments, handler):
        return self.subscribe_depth(instruments, handler)

    def unsubscribe_instruments(self, instruments):
        with self._lock:
            self.unsubscribed_instruments.extend(str(item.instrument_id) for item in instruments)

    def unsubscribe(self, subscription):
        return None

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def build_universe() -> list[Any]:
    option_expiry = date.today() + timedelta(days=3)
    future_expiry = date.today() + timedelta(days=17)
    return [
        equity("RELIANCE"),
        equity("TCS"),
        make_index("NIFTY"),
        Option.of("NFO", "NIFTY", option_expiry, 25000, "CE"),
        Option.of("NFO", "NIFTY", option_expiry, 25000, "PE"),
        Option.of("NFO", "RELIANCE", option_expiry, 1300, "CE"),
        Option.of("NFO", "RELIANCE", option_expiry, 1300, "PE"),
        Future.of("NFO", "NIFTY", future_expiry),
        Future.of("NFO", "RELIANCE", future_expiry),
    ]


def build_app(
    *,
    fail_rate: float,
    seed: int,
    rest_rate: float = 100_000.0,
    rest_burst: int = 100_000,
) -> tuple[Any, dict[str, ChaosBroker], list[Any]]:
    universe = build_universe()
    brokers = {
        "dhan": ChaosBroker("dhan", universe, fail_rate=fail_rate, seed=seed),
        "upstox": ChaosBroker("upstox", universe, fail_rate=fail_rate, seed=seed + 1),
    }
    runtimes = {name: BrokerRuntime(name, broker) for name, broker in brokers.items()}
    config = gateway_config(
        primary_live="dhan",
        primary_quotes="dhan",
        primary_history="dhan",
        primary_depth="dhan",
        primary_chain="dhan",
        rest_rate_per_second=rest_rate,
        rest_burst=rest_burst,
        stale_after_seconds=3600.0,
        max_subscriptions=200,
    )
    return create_app(config, runtimes), brokers, universe


# ---------------------------------------------------------------------------
# Randomized REST load
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Spec:
    kind: str
    payload: Any


def _build_specs(
    count: int,
    rng: random.Random,
    instruments: list[str],
    underlyings: list[str],
) -> list[_Spec]:
    specs: list[_Spec] = []
    for _ in range(count):
        roll = rng.random()
        if roll < 0.40:
            specs.append(_Spec("quote", rng.choice(instruments)))
        elif roll < 0.54:
            size = rng.randint(2, min(4, len(instruments)))
            specs.append(_Spec("quotes", rng.sample(instruments, k=size)))
        elif roll < 0.69:
            specs.append(
                _Spec(
                    "history",
                    (
                        rng.choice(instruments),
                        rng.choice(("1m", "5m", "15m", "1h")),
                        rng.randint(1, 5),
                        rng.randint(0, 59),
                    ),
                )
            )
        elif roll < 0.79:
            specs.append(
                _Spec(
                    "depth",
                    (rng.choice(instruments), rng.choice((5, 10, 20))),
                )
            )
        elif roll < 0.87:
            specs.append(
                _Spec(
                    "chain",
                    (rng.choice(underlyings), rng.choice((True, False))),
                )
            )
        elif roll < 0.93:
            specs.append(_Spec("futures", rng.choice(underlyings)))
        elif roll < 0.97:
            specs.append(_Spec("search", rng.choice(("NIFTY", "RELIANCE", "TCS"))))
        else:
            specs.append(_Spec("resolve", rng.choice(instruments)))
    return specs


async def _request_once(client: httpx.AsyncClient, spec: _Spec, now: datetime) -> httpx.Response:
    kind, payload = spec.kind, spec.payload
    if kind == "quote":
        return await client.get(f"/v1/quote/{payload}")
    if kind == "quotes":
        return await client.post("/v1/quotes", json={"instruments": payload})
    if kind == "history":
        instrument, timeframe, days, minutes = payload
        end = now - timedelta(minutes=minutes)
        start = end - timedelta(days=days)
        return await client.get(
            f"/v1/history/{instrument}",
            params={
                "timeframe": timeframe,
                "from": start.isoformat(),
                "to": end.isoformat(),
                "source": "broker",
            },
        )
    if kind == "depth":
        instrument, levels = payload
        return await client.get(f"/v1/depth/{instrument}", params={"levels": levels})
    if kind == "chain":
        underlying, live = payload
        return await client.get(
            f"/v1/option-chain/{underlying}",
            params={"live": "true" if live else "false"},
        )
    if kind == "futures":
        return await client.get(f"/v1/futures/{payload}")
    if kind == "search":
        return await client.get("/v1/instruments/search", params={"q": payload})
    if kind == "resolve":
        return await client.get("/v1/instruments/resolve", params={"q": payload})
    raise AssertionError(f"unknown spec: {spec!r}")


def _error_code(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 - non-JSON bodies have no error code
        return None
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        return data["error"].get("code")
    return None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _broker_report(broker: ChaosBroker) -> dict[str, Any]:
    return {
        "calls": broker.calls,
        "failures": broker.failures,
        "by_method": dict(broker.call_counts),
        "failure_kinds": dict(broker.failure_kinds),
    }


async def run_load(
    *,
    requests: int = 300,
    concurrency: int = 20,
    seed: int = 1337,
    fail_rate: float = 0.20,
) -> dict[str, Any]:
    app, brokers, universe = build_app(fail_rate=fail_rate, seed=seed)
    rng = random.Random(seed)
    instruments = [str(item.instrument_id) for item in universe]
    underlyings = ["IDX:NIFTY", "NSE:RELIANCE"]
    specs = _build_specs(requests, rng, instruments, underlyings)
    now = datetime.now(UTC)
    queue: asyncio.Queue[tuple[int, _Spec]] = asyncio.Queue()
    for index, spec in enumerate(specs):
        queue.put_nowait((index, spec))
    results: list[tuple[int, str | None, float] | None] = [None] * requests
    started = time.perf_counter()

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:

            async def worker() -> None:
                while True:
                    try:
                        index, spec = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    start = time.perf_counter()
                    try:
                        response = await _request_once(client, spec, now)
                        results[index] = (
                            response.status_code,
                            _error_code(response),
                            time.perf_counter() - start,
                        )
                    except Exception as exc:  # noqa: BLE001 - transport failure is a result
                        results[index] = (
                            0,
                            f"transport:{type(exc).__name__}",
                            time.perf_counter() - start,
                        )

            await asyncio.gather(*(worker() for _ in range(concurrency)))

    duration = time.perf_counter() - started
    completed = [item for item in results if item is not None]
    statuses = Counter(str(status) for status, _, _ in completed)
    error_codes = Counter(code for _, code, _ in completed if code)
    latencies_ms = [latency * 1000 for _, _, latency in completed]
    report = {
        "requests": requests,
        "completed": len(completed),
        "concurrency": concurrency,
        "seed": seed,
        "fail_rate": fail_rate,
        "duration_seconds": round(duration, 3),
        "statuses": dict(statuses),
        "error_codes": dict(error_codes),
        "successes": statuses.get("200", 0),
        "transport_errors": statuses.get("0", 0),
        "latency_ms": {
            "p50": _percentile(latencies_ms, 0.50),
            "p95": _percentile(latencies_ms, 0.95),
            "p99": _percentile(latencies_ms, 0.99),
            "max": max(latencies_ms) if latencies_ms else None,
        },
        "brokers": {name: _broker_report(broker) for name, broker in brokers.items()},
    }

    # Invariants: typed failures only, both brokers exercised, no 500s.
    assert len(completed) == requests
    assert report["transport_errors"] == 0
    assert statuses.get("500", 0) == 0
    unknown = set(error_codes) - _KNOWN_ERROR_CODES
    assert not unknown, f"unknown error codes: {sorted(unknown)}"
    assert report["successes"] > 0
    assert brokers["dhan"].calls > 0
    assert brokers["upstox"].calls > 0
    assert brokers["dhan"].failures + brokers["upstox"].failures > 0
    return report


# ---------------------------------------------------------------------------
# Consumer rate limiting
# ---------------------------------------------------------------------------


async def run_consumer_rate_limit(*, seed: int = 7) -> dict[str, Any]:
    app, _, _ = build_app(fail_rate=0.0, seed=seed, rest_rate=0.001, rest_burst=1)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            first = await client.get("/v1/quote/IDX:NIFTY")
            second = await client.get("/v1/quote/IDX:NIFTY")
    report = {
        "first_status": first.status_code,
        "second_status": second.status_code,
        "second_code": _error_code(second),
        "retry_after": second.headers.get("Retry-After"),
    }
    assert report["first_status"] == 200
    assert report["second_status"] == 429
    assert report["second_code"] == "consumer_rate_limited"
    assert report["retry_after"] is not None
    return report


# ---------------------------------------------------------------------------
# Live-hub multiplexing
# ---------------------------------------------------------------------------


async def _next_frame(queue: asyncio.Queue, *, timeout: float = 2.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return queue.get_nowait()
        except asyncio.QueueEmpty:
            await asyncio.sleep(0.001)
    raise AssertionError("no WebSocket frame was fanned out")


async def run_hub_multiplexing(
    *, clients: int = 50, instruments: int = 9, seed: int = 7
) -> dict[str, Any]:
    app, brokers, universe = build_app(fail_rate=0.0, seed=seed)
    instrument_ids = [str(item.instrument_id) for item in universe[:instruments]]
    async with app.router.lifespan_context(app):
        hub = app.state.hub
        loop = asyncio.get_running_loop()
        queues: list[asyncio.Queue] = []
        for index in range(clients):
            queue: asyncio.Queue = asyncio.Queue()
            control: asyncio.Queue = asyncio.Queue()
            hub.add_client(f"c{index}", loop, queue, control)
            ack = hub.subscribe(f"c{index}", instrument_ids, "quote", 5)
            assert ack["op"] == "ack", ack
            queues.append(queue)

        assert brokers["dhan"].subscribe_quotes_calls == 1
        assert brokers["upstox"].subscribe_quotes_calls == 0

        instrument = app.state.directory.resolve(instrument_ids[0]).instrument
        brokers["dhan"].quote_handlers[0](make_quote(instrument))
        frames = [await _next_frame(queue) for queue in queues]
        assert all(frame["op"] == "quote" for frame in frames)

        for index in range(clients):
            hub.remove_client(f"c{index}")
        assert len(brokers["dhan"].unsubscribed_instruments) >= len(instrument_ids)
        status = hub.status()
        assert status["state"] == "halted"
        hub.stop()

    report = {
        "clients": clients,
        "instruments": len(instrument_ids),
        "upstream_subscribe_calls": brokers["dhan"].subscribe_quotes_calls,
        "upstox_subscribe_calls": brokers["upstox"].subscribe_quotes_calls,
        "clients_fanned_out": sum(1 for frame in frames if frame["op"] == "quote"),
        "unsubscribed": len(brokers["dhan"].unsubscribed_instruments),
        "status_after_unsubscribe": status["state"],
    }
    assert report["upstream_subscribe_calls"] == 1
    assert report["upstox_subscribe_calls"] == 0
    assert report["clients_fanned_out"] == clients
    assert report["unsubscribed"] >= len(instrument_ids)
    return report


# ---------------------------------------------------------------------------
# Degraded startup (one broker unavailable)
# ---------------------------------------------------------------------------


async def run_degraded_startup(*, seed: int = 11) -> dict[str, Any]:
    universe = build_universe()
    upstox = ChaosBroker("upstox", universe, fail_rate=0.0, seed=seed)
    runtimes = {
        "dhan": BrokerRuntime("dhan", None, error="missing credentials"),
        "upstox": BrokerRuntime("upstox", upstox),
    }
    config = gateway_config(
        primary_quotes="dhan",
        primary_history="dhan",
        primary_depth="dhan",
        primary_chain="dhan",
        rest_rate_per_second=100_000.0,
        rest_burst=100_000,
    )
    app = create_app(config, runtimes)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            health = await client.get("/v1/health")
            quote = await client.get("/v1/quote/IDX:NIFTY")
    report = {
        "ready": health.json().get("ready"),
        "quote_status": quote.status_code,
        "quote_broker": quote.json().get("broker") if quote.status_code == 200 else None,
    }
    assert report["ready"] is True
    assert report["quote_status"] == 200
    assert report["quote_broker"] == "upstox"
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def run_all(
    *,
    requests: int,
    concurrency: int,
    seed: int,
    fail_rate: float,
    skip_hub: bool = False,
    skip_rate_limit: bool = False,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "load": await run_load(
            requests=requests,
            concurrency=concurrency,
            seed=seed,
            fail_rate=fail_rate,
        ),
        "degraded_startup": await run_degraded_startup(seed=seed),
    }
    if not skip_rate_limit:
        report["consumer_rate_limit"] = await run_consumer_rate_limit(seed=seed)
    if not skip_hub:
        report["hub"] = await run_hub_multiplexing(seed=seed)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=25)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--fail-rate", type=float, default=0.20)
    parser.add_argument("--skip-hub", action="store_true")
    parser.add_argument("--skip-rate-limit", action="store_true")
    args = parser.parse_args(argv)
    report = asyncio.run(
        run_all(
            requests=args.requests,
            concurrency=args.concurrency,
            seed=args.seed,
            fail_rate=args.fail_rate,
            skip_hub=args.skip_hub,
            skip_rate_limit=args.skip_rate_limit,
        )
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
