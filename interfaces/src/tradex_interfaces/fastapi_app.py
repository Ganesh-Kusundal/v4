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
from tradex_interfaces.models import (  # noqa: F401  (re-exported)
    AccountResponse,
    ErrorDetail,
    ErrorResponse,
    HealthResponse,
    OrderResponse,
    PositionResponse,
)
from tradex_interfaces.queueing import (  # noqa: F401  (re-exported)
    CONTROL_QUEUE_MAX,
    OUTBOUND_QUEUE_MAX,
    _enqueue_control,
    _enqueue_drop_oldest,
)
from tradex_interfaces.replay_guard import ReplayGuard

#: One origin policy for the whole app: the CORS allow-list below and the CSRF
#: guard in ``auth.deps`` both read this, so they cannot drift apart.
from tradex_interfaces.auth.origin import resolve_ui_origin  # noqa: E402  (cycle-free leaf)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

#: Built frontend assets (``frontend/dist``), resolved relative to the repo
#: root. Extracted layout: ``tradex_interfaces`` → ``src`` → ``interfaces`` → repo.
#: The mount is opt-in by presence: no dist directory means API-only behavior.
_UI_DIST_DIR = (
    Path(__file__).resolve().parents[3] / "frontend" / "dist"
)


class _StateResolvedCORSMiddleware(CORSMiddleware):
    """``CORSMiddleware`` whose allow-list comes from the shared origin policy.

    Starlette instantiates middleware when the stack is first built (on the
    first request) and hands the constructor the *inner* app — the router, not
    the FastAPI instance — so the FastAPI object is passed separately as
    ``origin_source`` for ``app.state.ui_origin`` to be readable at that
    moment. Resolving here (not at ``add_middleware`` time) is what lets a
    caller who sets ``app.state.ui_origin`` programmatically get the same
    origin in CORS and in the CSRF guard.

    Everything else about the policy is unchanged: one explicit origin, never
    ``"*"``, all methods and headers.
    """

    def __init__(self, app: Any, origin_source: Any) -> None:
        super().__init__(
            app,
            allow_origins=[
                resolve_ui_origin(getattr(origin_source, "state", None))
            ],
            allow_methods=["*"],
            allow_headers=["*"],
        )


def create_app(
    session: Any | None = None,
    api_key: str | None = None,
    outbound_max: int = OUTBOUND_QUEUE_MAX,
    replay_guard: ReplayGuard | None = None,
) -> FastAPI:
    """Create a FastAPI application backed by an optional TradingSession.

    ``outbound_max`` bounds each /ws/stream connection's outbound queue
    (drop-oldest on overflow) — small in tests to exercise backpressure.
    """
    app = FastAPI(title="TradeX v4 API", version="0.1.0")
    app.state.replay_guard = (
        replay_guard if replay_guard is not None else ReplayGuard()
    )

    # -- G13 typed-error contract -----------------------------------------------
    _STATUS_CODE_MAP: dict[int, str] = {
        400: "bad_request",
        403: "unauthorized",
        404: "not_found",
        409: "conflict",
        422: "validation",
        429: "rate_limited",
        500: "internal_error",
        502: "upstream_unavailable",
        503: "no_session",
    }

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(
        request: Request, exc: HTTPException
    ) -> JSONResponse:
        code = _STATUS_CODE_MAP.get(exc.status_code, "error")
        # Forward exception-level headers (e.g. Retry-After from 429) so
        # they reach the client. JSONResponse accepts headers as a plain dict.
        extra_headers: dict[str, str] | None = exc.headers or None
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": str(exc.detail)}},
            headers=extra_headers,
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
    #
    # Registered first, exactly as before, so this stays the outermost user
    # middleware. The allow-list itself is resolved when Starlette builds the
    # stack on the first request, from the same resolver the CSRF guard uses —
    # see _StateResolvedCORSMiddleware.
    app.add_middleware(_StateResolvedCORSMiddleware, origin_source=app)
    # Chart data plane (closed-bar history, indicator compute, strategy
    # backtests): one router the openalgo-charts frontend is built against.
    # Workspace persistence lives under runtime/ (gitignored state).
    from tradex_interfaces.routes.chart import create_chart_router

    workspace_db = os.environ.get("TRADEX_WORKSPACE_DB")
    if workspace_db is None:
        # One runtime root, cwd-independent. ``default_runtime_dir`` honours
        # TRADEX_RUNTIME_DIR and otherwise anchors to the repo from ``__file__``
        # — the same seam broker token/totp state uses. The old fallback here
        # was a bare ``"runtime"`` (cwd-relative), so launching the API from a
        # different directory silently forked workspace.sqlite.
        from tradex_brokers.common.paths import default_runtime_dir

        workspace_db = str(default_runtime_dir() / "workspace.sqlite")
    app.include_router(create_chart_router(session, workspace_db_path=workspace_db))
    app.state.session = session
    app.state.api_key = api_key

    # -- Session store and auth routes (B8) ------------------------------------
    # Always created; when no api_key is configured the store is present but
    # the login endpoint returns 503 (no credentials configured).
    from tradex_interfaces.auth.session_store import InMemorySessionStore
    from tradex_interfaces.auth.routes import router as _auth_router
    from tradex_interfaces.auth.rate_limit import InMemoryRateLimiter

    app.state.session_store = InMemorySessionStore()
    # The one origin for this app. Assigned before the first request, which is
    # when Starlette builds the middleware stack and resolves the CORS
    # allow-list — so CORS and CSRF read the same string.
    app.state.ui_origin = resolve_ui_origin(app.state)
    # C5: rate limiter for money commands; stored on app.state so tests get an
    # isolated instance per app and the dep reads it at request time.
    app.state.rate_limiter = InMemoryRateLimiter()
    app.include_router(_auth_router)
    # Per-app refcounting registry: shares the session's live MarketFeed
    # across every /ws/stream connection. None for paper/no-feed sessions.
    app.state.feed_registry = None
    if session is not None:
        from tradex_runtime.market_feed import FeedRegistry, MarketFeed

        feed = getattr(session, "market_feed", None)
        if isinstance(feed, MarketFeed):
            app.state.feed_registry = FeedRegistry(feed)
    # Single source of truth for depth-mode normalization (shared with
    # MarketFeed/FeedRegistry) — imported once, not per WebSocket connection.
    # -- HTTP routes (each module owns its slice) ----------------------------
    # Stream route is intentionally NOT included — the WebSocket closure
    # depends on app.state.feed_registry + normalize_depth + outbound_max
    # and stays in this function below.
    from tradex_interfaces.routes import (
        account as _account_route,
    )
    from tradex_interfaces.routes import (
        extensions as _extensions_route,
    )
    from tradex_interfaces.routes import (
        health as _health_route,
    )
    from tradex_interfaces.routes import (
        market_data as _market_data_route,
    )
    from tradex_interfaces.routes import (
        orders as _orders_route,
    )
    from tradex_interfaces.routes import (
        portfolio as _portfolio_route,
    )
    from tradex_runtime.market_feed import normalize_depth

    # C5: rate_limit_money is method-guarded (no-op on GET) so wiring it here
    # at include_router level applies to all order routes safely.
    from fastapi import Depends as _Depends
    from tradex_interfaces.auth.rate_limit import rate_limit_money as _rlm

    app.include_router(
        _orders_route.create_orders_router(replay_guard=app.state.replay_guard),
        dependencies=[_Depends(_rlm)],
    )
    for _r in (
        _health_route.router,
        _portfolio_route.router,
        _market_data_route.router,
        _account_route.router,
        _extensions_route.router,
    ):
        app.include_router(_r)

    # --- WebSocket (ReactiveBus bridge) ----------------------------------------
    # Delegated to routes.stream.ws_stream — closure state is now explicit.
    #
    # B8 auth gate: authentication is evaluated HERE, before calling ws_stream,
    # so stream.py (owned by B6) does not need modification.
    #
    # Three paths (checked in order):
    #   1. Session cookie (new)   — validated against app.state.session_store
    #   2. ?api_key= query param  — legacy/loopback-dev, constant-time compare
    #   3. No auth configured     — dev/paper mode, allow through
    #
    # When path (1) succeeds, ws_stream is called via _AuthProxyApp which
    # presents api_key=None — this prevents stream.py's own redundant key check
    # from closing a legitimately session-authenticated connection.  All other
    # app state (session, feed_registry, metrics, …) is delegated unchanged.
    #
    # B6 integration note: stream.py currently contains its own api_key check
    # (lines 88–95 of routes/stream.py).  That check is harmless when the
    # legacy path is used (both layers validate the same key) and is bypassed
    # via _AuthProxyApp when the session path is used.  When B6 refactors
    # stream.py, it should replace that check with a call to
    # ``auth.ws_validate(app, ws)`` and remove the query-string credential path.

    class _AuthProxyState:
        """Delegate all state attributes but override api_key to None.

        Used to tell stream.py's internal auth check that auth was already
        handled at the gateway layer (session-cookie path).
        """

        def __init__(self, real_state: Any) -> None:
            self._real = real_state

        @property
        def api_key(self) -> None:  # noqa: D401 – property, not function
            return None  # auth already validated; stream.py should skip its check

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

    class _AuthProxyApp:
        """Minimal app proxy that exposes api_key=None to stream.py."""

        def __init__(self, real_app: Any) -> None:
            self._real = real_app
            self.state = _AuthProxyState(real_app.state)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

    @app.websocket("/ws/stream")
    async def _ws_stream_endpoint(ws: WebSocket) -> None:
        import secrets as _secrets
        from urllib.parse import parse_qs

        from tradex_interfaces.routes.stream import ws_stream

        _store = getattr(app.state, "session_store", None)
        _expected_key = getattr(app.state, "api_key", None)
        _authenticated = False
        _via_session = False

        # Path 1: session cookie (browser same-origin upgrade sends it automatically)
        if _store is not None:
            _raw_id = ws.cookies.get("tradex_session")
            if _raw_id:
                _record = _store.lookup(_raw_id)
                if _record is not None:
                    _authenticated = True
                    _via_session = True

        # Path 2: legacy API key in query string (loopback-dev / programmatic)
        if not _authenticated and _expected_key is not None:
            _provided = parse_qs(ws.url.query).get("api_key", [None])[0]
            if _provided and _secrets.compare_digest(str(_provided), str(_expected_key)):
                _authenticated = True

        # Reject when auth is configured but neither credential was valid
        if not _authenticated and _expected_key is not None:
            await ws.close(code=1008, reason="authentication required")
            return

        # Use proxy app for session-cookie path so stream.py skips its redundant check
        _effective_app = _AuthProxyApp(app) if _via_session else app

        await ws_stream(
            _effective_app,
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
    from tradex_runtime.metrics import MetricsRegistry

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
from tradex_interfaces._helpers import (  # noqa: E402
    is_ready as _is_ready,
)
from tradex_interfaces._helpers import (  # noqa: E402
    readiness as _readiness,
)

#: Serve-spec environment keys — how the importable ASGI factory below learns
#: how to rebuild a session inside a uvicorn-spawned subprocess (workers /
#: reload). Environment variables are inherited across the spawn boundary;
#: an in-memory TradingSession object is not (it holds threads/sockets).
_SERVE_ENV_BROKER = "TRADEX_SERVE_BROKER"
_SERVE_ENV_MODE = "TRADEX_SERVE_MODE"
_SERVE_ENV_API_KEY = "TRADEX_SERVE_API_KEY"
_SERVE_ENV_HOST = "TRADEX_SERVE_HOST"


def _normalize_session_mode(mode: str | None) -> str:
    normalized = (mode or "paper").strip().lower()
    if normalized not in {"paper", "backtest", "replay", "live"}:
        raise ValueError(f"unknown session mode {mode!r}")
    return normalized


def _normalize_bind_host(host: str) -> str:
    return host.strip().lower()


def _require_api_key_for_live(mode: str | None, api_key: str | None) -> None:
    """Require authentication for live mode, independent of broker identity."""
    if _normalize_session_mode(mode) == "live" and not api_key:
        raise ValueError(
            "live mode requires an API key (set TRADEX_SERVE_API_KEY / "
            "pass api_key=)"
        )


def _require_live_bind_loopback(
    broker: str | None,
    mode: str | None,
    host: str,
) -> None:
    if _normalize_session_mode(mode) != "live":
        return

    broker_id = (broker or "PAPER").strip().upper()
    normalized_host = _normalize_bind_host(host)
    if normalized_host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError(
            f"live broker {broker_id!r} cannot bind to non-loopback host {host!r}; "
            "use a loopback host such as 127.0.0.1, ::1, or localhost until a "
            "trusted authentication layer is available"
        )


def serve_app() -> FastAPI:
    """Build the spawned worker/reload app from the controlled serve spec.

    The supported entrypoint is :func:`start_fastapi_server` (and therefore
    ``tradex serve``). A direct ``uvicorn tradex_interfaces.fastapi_app:serve_app``
    command cannot expose its actual bind host to this factory, so direct
    factory invocation is outside enforcement of the loopback policy.
    """
    import os

    from tradex_domain import BrokerId

    from tradex_runtime.session import TradingSession

    broker = os.environ.get(_SERVE_ENV_BROKER, "PAPER").strip().upper()
    mode = _normalize_session_mode(os.environ.get(_SERVE_ENV_MODE, "live"))
    api_key = os.environ.get(_SERVE_ENV_API_KEY) or None
    host = _normalize_bind_host(os.environ.get(_SERVE_ENV_HOST, "127.0.0.1"))
    _require_api_key_for_live(mode, api_key)
    _require_live_bind_loopback(broker, mode, host)

    if mode == "paper":
        session = TradingSession.paper(broker_id=broker)
    elif mode == "live":
        session = TradingSession.live(BrokerId(broker), confirm=True)
    else:
        from tradex_config.schema import AppConfig
        from tradex_runtime.startup import boot

        session = boot(
            AppConfig(broker_id=BrokerId(broker), mode=mode),
            wire_strategies=False,
        )
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
    ).strip().upper()
    raw_mode = getattr(session, "mode", None) if session is not None else "paper"
    mode = _normalize_session_mode(raw_mode if isinstance(raw_mode, str) else "live")
    normalized_host = _normalize_bind_host(host)
    _require_api_key_for_live(mode, api_key)
    _require_live_bind_loopback(broker_id, mode, normalized_host)
    if (workers > 1 or reload) and mode == "live":
        # Every spawned worker/reload process rebuilds its own session via
        # serve_app(), which for a live broker means a fresh token/auth flow
        # per process (interactive TOTP, cooldowns, concurrent logins).
        raise ValueError(
            f"--workers/--reload require a non-live session "
            f"(got mode={mode}, broker={broker_id}); live brokers re-authenticate "
            "per worker process, so scale with separate `tradex serve` instances "
            "on different ports instead"
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
        os.environ[_SERVE_ENV_MODE] = mode
        os.environ[_SERVE_ENV_HOST] = normalized_host
        if api_key is not None:
            os.environ[_SERVE_ENV_API_KEY] = api_key
        else:
            os.environ.pop(_SERVE_ENV_API_KEY, None)
        uvicorn.run(
            "tradex_interfaces.fastapi_app:serve_app",
            factory=True,
            host=normalized_host,
            port=port,
            workers=workers,
            reload=reload,
        )
        return

    # Single-process path: no serve spec needed — leave the environment clean
    # so a later factory-mode serve in this process cannot read a stale spec.
    os.environ.pop(_SERVE_ENV_BROKER, None)
    os.environ.pop(_SERVE_ENV_MODE, None)
    os.environ.pop(_SERVE_ENV_API_KEY, None)
    os.environ.pop(_SERVE_ENV_HOST, None)
    app = create_app(session, api_key=api_key)
    uvicorn.run(app, host=normalized_host, port=port)
