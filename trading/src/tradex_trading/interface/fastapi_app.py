"""TradeX v4 HTTP API — FastAPI with CORS, OpenAPI, and WebSocket support."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.responses import Response

# Pydantic response models and WebSocket queue helpers live in sibling
# modules. They are re-exported from here so existing callers and tests that
# import them off ``fastapi_app`` keep working; new code should import
# directly from the sibling modules.
from tradex_trading.interface.models import (  # noqa: F401  (re-exported)
    AccountResponse,
    ErrorDetail,
    ErrorResponse,
    HealthResponse,
    OrderResponse,
    PositionResponse,
)
from tradex_trading.interface.queueing import (  # noqa: F401  (re-exported)
    CONTROL_QUEUE_MAX,
    OUTBOUND_QUEUE_MAX,
    _enqueue_control_drop_oldest,
    _enqueue_drop_oldest,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

#: Built frontend assets (``frontend/dist``), resolved relative to the repo
#: root two levels up from this package's ``interface/`` directory. The mount
#: is opt-in by presence: no dist directory means API-only behavior, exactly
#: as before, so a source checkout without a built frontend serves nothing
#: extra and tests never depend on a node build having run.
_UI_DIST_DIR = (
    Path(__file__).resolve().parents[4] / "frontend" / "dist"
)


def create_app(
    session: Any | None = None,
    api_key: str | None = None,
    outbound_max: int = OUTBOUND_QUEUE_MAX,
) -> FastAPI:
    """Create a FastAPI application backed by an optional TradingSession.

    ``outbound_max`` bounds each /ws/stream connection's outbound queue
    (drop-oldest on overflow) — small in tests to exercise backpressure.
    """
    app = FastAPI(title="TradeX v4 API", version="0.1.0")

    # -- G13 typed-error contract -----------------------------------------------
    _STATUS_CODE_MAP: dict[int, str] = {
        400: "bad_request",
        403: "unauthorized",
        404: "not_found",
        409: "conflict",
        422: "validation",
        500: "internal_error",
        502: "upstream_unavailable",
        503: "no_session",
    }

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(
        request: Request, exc: HTTPException
    ) -> JSONResponse:
        code = _STATUS_CODE_MAP.get(exc.status_code, "error")
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": str(exc.detail)}},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "validation", "message": str(exc)}},
        )

    # CORS: lock to the served UI origin. When dist/ is mounted the UI and API
    # share an origin (single-origin prod); in dev Vite proxies /api + /ws to
    # 8000 so the browser origin is localhost:5173. Allow only that host set,
    # never "*" now that signed API access exists.
    _origin = os.environ.get("TRADEX_UI_ORIGIN") or "http://localhost:5173"
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[_origin],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Chart data plane (closed-bar history, indicator compute, strategy
    # backtests): one router the openalgo-charts frontend is built against.
    from tradex_trading.interface.routes.chart import create_chart_router

    app.include_router(create_chart_router(session))
    app.state.session = session
    app.state.api_key = api_key
    # Per-app refcounting registry: shares the session's live MarketFeed
    # across every /ws/stream connection. None for paper/no-feed sessions.
    app.state.feed_registry = None
    if session is not None:
        from tradex_trading.runtime.market_feed import FeedRegistry, MarketFeed

        feed = getattr(session, "market_feed", None)
        if isinstance(feed, MarketFeed):
            app.state.feed_registry = FeedRegistry(feed)
    # Single source of truth for depth-mode normalization (shared with
    # MarketFeed/FeedRegistry) — imported once, not per WebSocket connection.
    # -- HTTP routes (each module owns its slice) ----------------------------
    # Stream route is intentionally NOT included — the WebSocket closure
    # depends on app.state.feed_registry + normalize_depth + outbound_max
    # and stays in this function below.
    from tradex_trading.interface.routes import (
        account as _account_route,
    )
    from tradex_trading.interface.routes import (
        extensions as _extensions_route,
    )
    from tradex_trading.interface.routes import (
        health as _health_route,
    )
    from tradex_trading.interface.routes import (
        market_data as _market_data_route,
    )
    from tradex_trading.interface.routes import (
        orders as _orders_route,
    )
    from tradex_trading.interface.routes import (
        portfolio as _portfolio_route,
    )
    from tradex_trading.runtime.market_feed import normalize_depth

    for _r in (
        _health_route.router,
        _portfolio_route.router,
        _orders_route.router,
        _market_data_route.router,
        _account_route.router,
        _extensions_route.router,
    ):
        app.include_router(_r)

    # --- WebSocket (ReactiveBus bridge) ----------------------------------------
    # Delegated to routes.stream.ws_stream — closure state is now explicit.

    @app.websocket("/ws/stream")
    async def _ws_stream_endpoint(ws: WebSocket) -> None:
        from tradex_trading.interface.routes.stream import ws_stream

        await ws_stream(
            app,
            ws,
            session=app.state.session,
            registry=app.state.feed_registry,
            outbound_max=outbound_max,
            normalize_depth=normalize_depth,
        )


    # Built frontend, when present: single origin for API + UI (no CORS in
    # production). Absent dist = API-only app, byte-identical to before.
    if _UI_DIST_DIR.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/ui", StaticFiles(directory=_UI_DIST_DIR, html=True), name="ui")

        # Inject the API key into index.html at request time so the SPA's WS
        # layer can authenticate. Read-only: paper/dev have no key -> empty content.
        @app.get("/", response_class=HTMLResponse)
        async def _root_with_key() -> str:  # noqa: ANN001
            html = (_UI_DIST_DIR / "index.html").read_text()
            key = getattr(app.state, "api_key", None) or ""
            return html.replace(
                'content="" data-applied="false"',
                f'content="{key}" data-applied="true"',
            )

    # -- Prometheus metrics exposition (G14) -----------------------------------
    from tradex_trading.runtime.metrics import MetricsRegistry

    # Prefer the boot-time registry carried by the session (real runtime
    # counters: bus drops, engine errors); fall back to a fresh one for
    # session-less test apps. isinstance-guarded so MagicMock sessions in
    # tests never leak a fake registry into /metrics.
    _session_metrics = getattr(session, "metrics", None) if session is not None else None
    if not isinstance(_session_metrics, MetricsRegistry):
        _session_metrics = None
    app.state.metrics = _session_metrics or MetricsRegistry()

    # Return a raw Response (annotated with the module-level class so the
    # forward ref resolves): FastAPI skips response-model schema generation
    # for Response subclasses, keeping /openapi.json intact.
    @app.get("/metrics")
    async def _metrics_endpoint() -> Response:
        return Response(
            content=app.state.metrics.render_prometheus(),
            media_type="text/plain; version=0.0.4",
        )

    return app


# Readiness probe helpers live in :mod:`tradex_trading.interface._helpers`
# (imported here, mid-file, so route modules can import them without a
# cycle); used by ``start_fastapi_server`` below to refuse a raw session.
from tradex_trading.interface._helpers import (  # noqa: E402
    is_ready as _is_ready,
)
from tradex_trading.interface._helpers import (  # noqa: E402
    readiness as _readiness,
)

#: Serve-spec environment keys — how the importable ASGI factory below learns
#: how to rebuild a session inside a uvicorn-spawned subprocess (workers /
#: reload). Environment variables are inherited across the spawn boundary;
#: an in-memory TradingSession object is not (it holds threads/sockets).
_SERVE_ENV_BROKER = "TRADEX_SERVE_BROKER"
_SERVE_ENV_API_KEY = "TRADEX_SERVE_API_KEY"


def _require_api_key_for_live(broker: str | None, api_key: str | None) -> None:
    """Refuse to serve a live broker without an API key (fail-closed).

    Paper is the dev path and stays keyless. Any live broker must be
    reachable only through an authenticated endpoint — an unset key means
    the write/order routes would run unauthenticated, which is unacceptable
    for real money.
    """
    broker = (broker or "PAPER").upper()
    if broker != "PAPER" and not api_key:
        raise ValueError(
            f"live broker {broker!r} requires an API key (set "
            f"TRADEX_SERVE_API_KEY / pass api_key=)"
        )


def serve_app() -> FastAPI:
    """Importable ASGI app factory used for uvicorn ``workers`` / ``reload``.

    uvicorn >= 0.51 starts worker/reload subprocesses with the ``spawn``
    multiprocessing context, which pickles the uvicorn Config — an in-memory
    ``TradingSession`` (threads, sockets, locks) cannot cross that boundary,
    and a re-imported module cannot see the parent process's objects either.
    So the serve spec (broker + optional API key) travels in the environment
    (inherited by spawned children) and each process rebuilds its own READY
    session here.
    """
    import os

    from tradex_domain import BrokerId

    from tradex_trading.sdk.session import TradingSession

    broker = os.environ.get(_SERVE_ENV_BROKER, "PAPER").upper()
    api_key = os.environ.get(_SERVE_ENV_API_KEY) or None
    _require_api_key_for_live(broker, api_key)
    if broker == "PAPER":
        session = TradingSession.paper()
    else:
        session = TradingSession.live(BrokerId(broker), confirm=True)
    return create_app(session, api_key=api_key)


def start_fastapi_server(
    session: Any = None,
    host: str = "127.0.0.1",
    port: int = 8080,
    api_key: str | None = None,
    workers: int = 1,
    reload: bool = False,
) -> None:
    """Start a uvicorn server with the FastAPI app.

    Runs a pre-bind readiness probe (the same check /health/ready performs)
    and refuses to bind when the session is present but not READY. ``workers``
    and ``reload`` are forwarded to uvicorn; either one switches to the
    importable :func:`serve_app` factory because uvicorn spawns subprocesses
    for them (an in-memory session cannot be pickled into the child).
    """
    import os

    import uvicorn

    if workers < 1:
        raise ValueError(f"workers must be >= 1, got {workers}")

    broker_id = str(
        getattr(session, "broker_id", "PAPER") if session is not None else "PAPER"
    ).upper()
    _require_api_key_for_live(broker_id, api_key)
    if (workers > 1 or reload) and broker_id != "PAPER":
        # Every spawned worker/reload process rebuilds its own session via
        # serve_app(), which for a live broker means a fresh token/auth flow
        # per process (interactive TOTP, cooldowns, concurrent logins).
        raise ValueError(
            f"--workers/--reload require a paper session (got broker={broker_id}); "
            "live brokers re-authenticate per worker process, so scale with "
            "separate `tradex serve` instances on different ports instead"
        )

    # Pre-bind readiness probe — mirrors /health/ready. A raw (NEW) session
    # must not start accepting traffic.
    ready = _readiness(session)
    if not _is_ready(session):
        raise ValueError(
            f"session not ready (state={ready.session_state}); "
            "call session.start() before serve"
        )

    if workers > 1 or reload:
        # uvicorn 0.51 spawns workers/reload subprocesses and pickles the
        # Config — carry the spec in the env (inherited by spawn children)
        # and let each process build its own session via serve_app().
        os.environ[_SERVE_ENV_BROKER] = broker_id
        if api_key is not None:
            os.environ[_SERVE_ENV_API_KEY] = api_key
        else:
            os.environ.pop(_SERVE_ENV_API_KEY, None)
        uvicorn.run(
            "tradex_trading.interface.fastapi_app:serve_app",
            factory=True,
            host=host,
            port=port,
            workers=workers,
            reload=reload,
        )
        return

    # Single-process path: no serve spec needed — leave the environment clean
    # so a later factory-mode serve in this process cannot read a stale spec.
    os.environ.pop(_SERVE_ENV_BROKER, None)
    os.environ.pop(_SERVE_ENV_API_KEY, None)
    app = create_app(session, api_key=api_key)
    uvicorn.run(app, host=host, port=port)
