"""FastAPI application factory and the REST/WS surface."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import TypeAdapter, ValidationError

from tradex_gateway.auth import TokenBucket, check_api_key, identity_for
from tradex_gateway.brokers import BrokerRuntime, build_runtimes
from tradex_gateway.config import GatewayConfig, load_local_env
from tradex_gateway.directory import InstrumentDirectory
from tradex_gateway.errors import (
    BadRequest,
    ConsumerRateLimited,
    GatewayError,
    Unauthorized,
)
from tradex_gateway.history import HistoryService
from tradex_gateway.live import LiveHub
from tradex_gateway.market import ChainService, DepthService, QuoteService
from tradex_gateway.models import (
    ClientMessage,
    HistoryBatchRequest,
    PingMessage,
    QuotesRequest,
    StatusMessage,
    SubscribeMessage,
    UnsubscribeMessage,
)
from tradex_gateway.router import MarketDataRouter

log = logging.getLogger(__name__)

_CLIENT_MESSAGE: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


class _Metrics:
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + amount

    def render(self) -> str:
        with self._lock:
            rows = [
                f"tradex_gateway_{name} {value}" for name, value in sorted(self._counts.items())
            ]
        return "\n".join(rows) + "\n"


def create_app(
    config: GatewayConfig | None = None,
    runtimes: dict[str, BrokerRuntime] | None = None,
) -> FastAPI:
    cfg = (config or GatewayConfig.from_env()).validate()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.executor = ThreadPoolExecutor(
            max_workers=cfg.broker_workers, thread_name_prefix="gateway-broker"
        )
        if app.state.runtimes is None:
            load_local_env()
            app.state.runtimes = await _run(app.state, build_runtimes, cfg)
        _wire(app)
        try:
            yield
        finally:
            app.state.hub.stop()
            for runtime in app.state.runtimes.values():
                runtime.close()
            app.state.executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="TradeX Broker Gateway", version="0.1.0", lifespan=lifespan)
    app.state.config = cfg
    app.state.runtimes = runtimes
    app.state.metrics = _Metrics()
    app.state.limiter = TokenBucket(cfg.rest_rate_per_second, cfg.rest_burst)
    app.state.run = partial(_run, app.state)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path == "/v1/health" or not (path.startswith("/v1/") or path == "/metrics"):
            return await call_next(request)
        provided = request.headers.get("x-api-key")
        try:
            check_api_key(provided, cfg)
        except GatewayError as exc:
            return _json(exc)
        if path == "/metrics":
            # Scrapes are authenticated but never consume consumer quota.
            return await call_next(request)
        identity = identity_for(
            provided, request.client.host if request.client is not None else None
        )
        allowed, retry_after = app.state.limiter.consume(identity)
        if not allowed:
            return _json(
                ConsumerRateLimited(
                    "consumer rate limit exceeded", retry_after=round(retry_after, 2)
                )
            )
        app.state.metrics.inc("http_requests_total")
        return await call_next(request)

    @app.exception_handler(GatewayError)
    async def gateway_error(_: Request, exc: GatewayError):
        return _json(exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        return JSONResponse(
            {
                "error": {
                    "code": "invalid_request",
                    "message": str(exc),
                    "broker": None,
                    "retryable": False,
                    "retry_after": None,
                }
            },
            status_code=400,
        )

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):
        log.exception("unhandled gateway error")
        return JSONResponse(
            {
                "error": {
                    "code": "internal_error",
                    "message": "internal server error",
                    "broker": None,
                    "retryable": False,
                    "retry_after": None,
                }
            },
            status_code=500,
        )

    @app.get("/v1/health")
    async def health(request: Request) -> dict[str, Any]:
        runtimes_now: dict[str, BrokerRuntime] = request.app.state.runtimes or {}
        ready = any(runtime.available for runtime in runtimes_now.values())
        return {
            "status": "ok" if ready else "degraded",
            "ready": ready,
            "brokers": [
                {
                    "name": name,
                    "available": runtime.available,
                    "error": runtime.error,
                    "health": runtime.health.snapshot(),
                }
                for name, runtime in runtimes_now.items()
            ],
            "feed": request.app.state.hub.status(),
        }

    @app.get("/v1/brokers")
    async def brokers(request: Request) -> dict[str, Any]:
        runtimes_now: dict[str, BrokerRuntime] = request.app.state.runtimes or {}
        return {
            "brokers": [
                {
                    "name": name,
                    "available": runtime.available,
                    "error": runtime.error,
                    "capabilities": _caps(runtime),
                    "health": runtime.health.snapshot(),
                }
                for name, runtime in runtimes_now.items()
            ],
            "primaries": {
                "live": cfg.primary_live,
                "quotes": cfg.primary_quotes,
                "history": cfg.primary_history,
                "depth": cfg.primary_depth,
                "chain": cfg.primary_chain,
            },
        }

    @app.get("/v1/instruments/search")
    async def search(
        request: Request,
        q: str,
        asset_class: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await request.app.state.run(
            request.app.state.directory.search,
            q,
            asset_class=asset_class,
            limit=max(1, min(limit, 200)),
        )

    @app.get("/v1/instruments/resolve")
    async def resolve(
        request: Request,
        q: str,
        asset_class: str | None = None,
    ) -> dict[str, Any]:
        resolved = await request.app.state.run(request.app.state.directory.resolve, q, asset_class)
        return {
            **InstrumentDirectory.instrument_meta(
                resolved.instrument, resolved.brokers, resolved.lot_sizes
            ),
            "per_broker": {
                name: str(instrument.instrument_id) if instrument is not None else None
                for name, instrument in resolved.per_broker.items()
            },
        }

    @app.get("/v1/history/{instrument_id}")
    async def history(
        request: Request,
        instrument_id: str,
        timeframe: str = "1m",
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
        source: str = "auto",
        broker: str | None = None,
        fill_gaps: bool = False,
    ) -> dict[str, Any]:
        end = _query_moment(to) if to else datetime.now(UTC)
        start = _query_moment(from_) if from_ else end - timedelta(days=5)
        return await request.app.state.run(
            request.app.state.history.history,
            instrument_id,
            timeframe,
            start,
            end,
            source=source,
            broker=broker,
            fill_gaps=fill_gaps,
        )

    @app.post("/v1/history/batch")
    async def history_batch(request: Request, payload: HistoryBatchRequest) -> list[dict[str, Any]]:
        requests = [item.model_dump() for item in payload.requests]
        return await request.app.state.run(
            request.app.state.history.batch, requests, fill_gaps=payload.fill_gaps
        )

    @app.get("/v1/quote/{instrument_id}")
    async def quote(
        request: Request, instrument_id: str, broker: str | None = None
    ) -> dict[str, Any]:
        return await request.app.state.run(request.app.state.quotes.quote, instrument_id, broker)

    @app.post("/v1/quotes")
    async def quotes(
        request: Request, payload: QuotesRequest, broker: str | None = None
    ) -> dict[str, Any]:
        return await request.app.state.run(
            request.app.state.quotes.quotes, payload.instruments, broker
        )

    @app.get("/v1/depth/{instrument_id}")
    async def depth(
        request: Request,
        instrument_id: str,
        levels: int = 5,
        broker: str | None = None,
    ) -> dict[str, Any]:
        if levels < 1 or levels > 30:
            raise BadRequest("levels must be between 1 and 30")
        return await request.app.state.run(
            request.app.state.depth.depth, instrument_id, levels, broker
        )

    @app.get("/v1/option-chain/{underlying}")
    async def option_chain(
        request: Request,
        underlying: str,
        expiry: str | None = None,
        live: bool = False,
        broker: str | None = None,
    ) -> dict[str, Any]:
        return await request.app.state.run(
            request.app.state.chains.option_chain,
            underlying,
            expiry,
            live=live,
            broker=broker,
        )

    @app.get("/v1/futures/{underlying}")
    async def futures(
        request: Request, underlying: str, broker: str | None = None
    ) -> dict[str, Any]:
        return await request.app.state.run(request.app.state.chains.futures, underlying, broker)

    @app.get("/metrics")
    async def metrics(request: Request) -> PlainTextResponse:
        return PlainTextResponse(request.app.state.metrics.render())

    @app.websocket("/v1/stream")
    async def stream(ws: WebSocket) -> None:
        provided = ws.query_params.get("api_key") or ws.headers.get("x-api-key")
        try:
            check_api_key(provided, cfg)
        except Unauthorized:
            await ws.close(code=1008)
            return
        await ws.accept()
        hub: LiveHub = ws.app.state.hub
        client_id = uuid.uuid4().hex
        queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
        control: asyncio.Queue = asyncio.Queue(maxsize=256)
        loop = asyncio.get_running_loop()
        hub.add_client(client_id, loop, queue, control)
        await queue.put({"op": "status", **hub.status()})

        async def writer() -> None:
            while True:
                if hub.close_requested(client_id):
                    await ws.close(code=1013)
                    return
                control_task = asyncio.create_task(control.get())
                tick_task = asyncio.create_task(queue.get())
                done, pending = await asyncio.wait(
                    {control_task, tick_task},
                    timeout=20,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if not done:
                    await ws.send_json({"op": "pong", "ts": _now_ms()})
                    continue
                frame = next(iter(done)).result()
                await ws.send_json(frame)

        async def reader() -> None:
            while True:
                try:
                    raw = await ws.receive_json()
                except (ValueError, KeyError, TypeError):
                    # JSONDecodeError is a ValueError; a binary frame yields no
                    # "text" key (KeyError) and an oversized int literal makes
                    # json.loads raise plain ValueError. None of them may tear
                    # down the connection. WebSocketDisconnect/RuntimeError are
                    # deliberately left uncaught so teardown still propagates.
                    await control.put(
                        {
                            "op": "error",
                            "code": "invalid_request",
                            "message": "invalid frame",
                        }
                    )
                    continue
                try:
                    message = _CLIENT_MESSAGE.validate_python(raw)
                except ValidationError as exc:
                    await control.put(
                        {
                            "op": "error",
                            "code": "invalid_request",
                            "message": exc.errors()[0]["msg"],
                        }
                    )
                    continue

                match message:
                    case SubscribeMessage():
                        ack = await ws.app.state.run(
                            hub.subscribe,
                            client_id,
                            message.instruments,
                            message.mode,
                            message.levels,
                        )
                        await control.put(ack)
                    case UnsubscribeMessage():
                        ack = await ws.app.state.run(
                            hub.unsubscribe,
                            client_id,
                            message.instruments,
                            message.subscription_id,
                        )
                        await control.put(ack)
                    case PingMessage():
                        await control.put({"op": "pong", "ts": _now_ms()})
                    case StatusMessage():
                        await control.put({"op": "status", **hub.status()})

        writer_task = asyncio.create_task(writer())
        reader_task = asyncio.create_task(reader())
        try:
            done, pending = await asyncio.wait(
                {writer_task, reader_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            for task in (writer_task, reader_task):
                task.cancel()
            await ws.app.state.run(hub.remove_client, client_id)

    return app


async def _run(state: Any, fn: Any, *args: Any, **kwargs: Any) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(state.executor, partial(fn, *args, **kwargs))


def _wire(app: FastAPI) -> None:
    runtimes: dict[str, BrokerRuntime] = app.state.runtimes
    cfg: GatewayConfig = app.state.config
    directory = InstrumentDirectory(runtimes)
    router = MarketDataRouter(cfg, runtimes, on_event=app.state.metrics.inc)
    quotes = QuoteService(directory, router)
    hub = LiveHub(cfg, router, directory, runtimes, on_event=app.state.metrics.inc)
    app.state.directory = directory
    app.state.router = router
    app.state.history = HistoryService(directory, router, cfg)
    app.state.quotes = quotes
    app.state.depth = DepthService(directory, router, live_depth=hub.latest_depth)
    app.state.chains = ChainService(directory, router, quotes)
    app.state.hub = hub


def _caps(runtime: BrokerRuntime) -> dict[str, Any] | None:
    capabilities = runtime.capabilities
    if capabilities is None:
        return None
    try:
        data = asdict(capabilities)
    except TypeError:
        return {
            key: getattr(capabilities, key) for key in dir(capabilities) if not key.startswith("_")
        }
    return {key: list(value) if isinstance(value, tuple) else value for key, value in data.items()}


def _json(exc: GatewayError) -> JSONResponse:
    headers: dict[str, str] = {}
    if exc.retry_after is not None:
        headers["Retry-After"] = str(max(1, int(exc.retry_after + 0.999)))
    return JSONResponse(exc.to_envelope(), status_code=exc.status, headers=headers)


def _query_moment(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadRequest(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise BadRequest("timestamp must include a UTC offset")
    return parsed


def _now_ms() -> int:
    return int(time.time() * 1000)
