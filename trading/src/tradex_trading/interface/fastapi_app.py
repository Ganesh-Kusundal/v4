"""TradeX v4 HTTP API — FastAPI with CORS, OpenAPI, and WebSocket support."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from tradex_domain.errors import CapabilityNotSupportedError

# Pydantic response models, WebSocket queue helpers, and the API-key auth
# dependency now live in sibling modules. They are re-exported from here so
# existing callers and tests that import them off ``fastapi_app`` keep
# working; new code should import directly from the sibling modules.
from tradex_trading.interface.auth import api_key_header, make_verify_api_key
from tradex_trading.interface.models import (  # noqa: F401  (re-exported)
    AccountResponse,
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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
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
    from tradex_trading.runtime.market_feed import normalize_depth

    # -- Auth dependency (built once per app via the auth module) -----------

    verify_api_key = make_verify_api_key(lambda: app.state)

    # -- HTTP routes (each module owns its slice) ----------------------------
    # Stream route is intentionally NOT included — the WebSocket closure
    # depends on app.state.feed_registry + normalize_depth + outbound_max
    # and stays in this function below.
    from tradex_trading.interface.routes import (
        account as _account_route,
        extensions as _extensions_route,
        health as _health_route,
        market_data as _market_data_route,
        orders as _orders_route,
        portfolio as _portfolio_route,
    )

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

    return app


# The actual definitions live in :mod:`tradex_trading.interface._helpers`
# (so route modules can import them without a cycle). Re-exported here
# for back-compat with code that imports them off fastapi_app.
from tradex_trading.interface._helpers import (  # noqa: F401
    is_ready as _is_ready,
    readiness as _readiness,
    serialize_position as _serialize_position,
)

# The actual definitions live in :mod:`tradex_trading.interface.routes._market_helpers`
# (so route modules can import them without a cycle). Re-exported here
# under their original underscore-prefixed names for back-compat with code
# that imports them off fastapi_app.
from tradex_trading.interface.routes._market_helpers import (  # noqa: F401
    enrich_chain_live as _enrich_chain_live,
    resolve_underlying_instrument as _resolve_underlying_instrument,
    serialize_option_chain as _serialize_option_chain,
)


#: Serve-spec environment keys — how the importable ASGI factory below learns
#: how to rebuild a session inside a uvicorn-spawned subprocess (workers /
#: reload). Environment variables are inherited across the spawn boundary;
#: an in-memory TradingSession object is not (it holds threads/sockets).
_SERVE_ENV_BROKER = "TRADEX_SERVE_BROKER"
_SERVE_ENV_API_KEY = "TRADEX_SERVE_API_KEY"


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
