"""FastAPI application factory: lifespan, guards, error envelopes, wiring."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from tradex_gateway.auth import TokenBucket, check_api_key, identity_for
from tradex_gateway.brokers import BrokerRuntime, build_runtimes
from tradex_gateway.config import GatewayConfig, load_local_env
from tradex_gateway.directory import InstrumentDirectory
from tradex_gateway.errors import ConsumerRateLimited, GatewayError
from tradex_gateway.history import HistoryService
from tradex_gateway.live import LiveHub
from tradex_gateway.market import ChainService, DepthService, QuoteService
from tradex_gateway.router import MarketDataRouter
from tradex_gateway.routes import (
    chains,
    depth,
    health,
    history,
    instruments,
    metrics,
    quotes,
    stream,
)
from tradex_gateway.routes.deps import _run

log = logging.getLogger(__name__)


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

    app.include_router(health.router)
    app.include_router(instruments.router)
    app.include_router(history.router)
    app.include_router(quotes.router)
    app.include_router(depth.router)
    app.include_router(chains.router)
    app.include_router(metrics.router)
    app.include_router(stream.router)

    return app


def _wire(app: FastAPI) -> None:
    runtimes: dict[str, BrokerRuntime] = app.state.runtimes
    cfg: GatewayConfig = app.state.config
    directory = InstrumentDirectory(runtimes)
    router = MarketDataRouter(cfg, runtimes, on_event=app.state.metrics.inc)
    quotes = QuoteService(directory, router)
    hub = LiveHub(cfg, router, directory, runtimes, on_event=app.state.metrics.inc)
    app.state.directory = directory
    app.state.router = router
    app.state.history = HistoryService(directory, router, cfg, executor=app.state.executor)
    app.state.quotes = quotes
    app.state.depth = DepthService(directory, router, live_depth=hub.latest_depth)
    app.state.chains = ChainService(directory, router, quotes)
    app.state.hub = hub


def _json(exc: GatewayError) -> JSONResponse:
    headers: dict[str, str] = {}
    if exc.retry_after is not None:
        headers["Retry-After"] = str(max(1, int(exc.retry_after + 0.999)))
    return JSONResponse(exc.to_envelope(), status_code=exc.status, headers=headers)
