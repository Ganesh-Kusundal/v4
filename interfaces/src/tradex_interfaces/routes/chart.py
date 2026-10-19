"""Chart data API — closed-bar history for the openalgo-charts frontend.

The backend is the only brain: bars come from the parquet datalake (offline,
rate-limit-free) with a broker fallback, resampling is single-sourced through
``HistoricalSeries.resample``, and timestamps are converted IST-naive -> UTC
seconds at this edge so the chart's gapless axis and its default
``Asia/Kolkata`` zone render IST wall clock without any client-side math.

Every bar served here is a *closed* bar: the forming bar belongs to the live
subscription (see ``runtime/bar_aggregator``), never to this endpoint.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from tradex_domain.constants import DEFAULT_INITIAL_CAPITAL
from tradex_domain.enums import Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.intervals import normalize_interval
from tradex_domain.timeframe import INTERVAL_TO_TIMEFRAME
from tradex_domain.timezones import IST_ZONE
from tradex_execution.projection import execution_projection

from tradex_interfaces.auth.deps import require_auth
from tradex_interfaces.safety import build_safety_report

#: Interval codes the chart may request, mapped to canonical Timeframes.
#: Single-sourced in the domain (:data:`tradex_domain.timeframe.
#: INTERVAL_TO_TIMEFRAME`) so this REST table and the WS/replay consumers
#: cannot drift; the alias keeps the historical route-local name. Deliberately
#: no monthly code: neither side has calendar-month bucketing yet (the chart
#: library refuses to guess that 'M' means 30 days), so offering it would
#: promise a bar we cannot build correctly.
INTERVAL_TIMEFRAME = INTERVAL_TO_TIMEFRAME

#: Default window when the caller passes no ``from``: ~10 trading days back,
#: enough for a first paint of any intraday interval; daily requests should
#: pass an explicit range.
_DEFAULT_LOOKBACK = timedelta(days=10)

#: Brief TTL cache for datalake hits, keyed on the full request. History is
#: immutable (closed bars), so a short TTL only bounds duplicate reads when a
#: user flips intervals back and forth; correctness never depends on it.
_CACHE_TTL_SECONDS = 30.0

#: Domain OrderStatus -> chart trade-tier status. Exhaustive over the enum:
#: a future member arriving must fail loudly here rather than silently render
#: as undefined in the chart's order lines.
_ORDER_STATUS_MAP: dict[str, str] = {
    "NEW": "pending",
    "PENDING": "pending",
    "SUBMITTED": "pending",
    "ACK": "working",
    "PARTIALLY_FILLED": "partial",
    "FILLED": "filled",
    "CANCELLED": "cancelled",
    "REJECTED": "rejected",
    # F-44: UNKNOWN gets its own tier. It is a broker row the process cannot
    # interpret, so the outcome is genuinely unknown — which is a *different*
    # answer from REJECTED, where the venue decided. The two were collapsed
    # here, and this is the surface an operator reads to decide whether to
    # flatten a position: "we do not know" and "the venue said no" call for
    # opposite actions. The distinction is already carried by the backend
    # (``/api/charts/safety`` ages UNKNOWN rows and counts unresolved ones);
    # the chart was the one surface that dropped it. Presentation only — no
    # engine state changes with it.
    "UNKNOWN": "unknown",
}


def map_order_status(status: Any) -> str:
    """Map one domain OrderStatus to the chart's status union.

    Raises on unmapped values so a new enum member fails at this seam, not
    as undefined behavior in the browser.
    """
    key = str(getattr(status, "value", status)).upper()
    mapped = _ORDER_STATUS_MAP.get(key)
    if mapped is None:
        raise ValueError(f"unmapped OrderStatus: {key!r}")
    return mapped


#: Domain OrderType -> chart order type. Exhaustive over the enum (MARKET,
#: LIMIT, STOP, STOP_LIMIT): a future member must fail loudly here rather
#: than render as a wrong 'LIMIT' line in the chart's book.
_ORDER_TYPE_MAP: dict[str, str] = {
    "MARKET": "MARKET",
    "LIMIT": "LIMIT",
    "STOP": "SL",
    "STOP_LIMIT": "SL-M",
}


def _map_order_type(order: Any) -> str:
    """Domain OrderType -> chart order type ('MARKET'|'LIMIT'|'SL'|'SL-M')."""
    raw = getattr(order.order_type, "value", str(order.order_type)).upper()
    mapped = _ORDER_TYPE_MAP.get(raw)
    if mapped is None:
        raise ValueError(f"unmapped OrderType: {raw!r}")
    return mapped


_STRATEGY_PARAM_TYPES: dict[str, str] = {
    "fast": "int",
    "slow": "int",
    "period": "int",
    "lookback": "int",
    "atr_period": "int",
    "stop_atr": "float",
    "reward": "float",
}

#: Default values for strategy parameters, keyed by strategy then param.
#: Sourced from each strategy's ``__init__`` defaults so the UI and the engine
#: agree on what a fresh pane runs without the user touching a slider.
_STRATEGY_DEFAULTS: dict[str, dict[str, float]] = {
    "sma_cross": {"fast": 5, "slow": 20},
    "mean_reversion": {"period": 14},
    "bracket_breakout": {"lookback": 20, "atr_period": 14, "stop_atr": 1.5, "reward": 2.0},
}

#: name -> module path under strategy.extensions.strategies.
_STRATEGY_FACTORIES: dict[str, str] = {
    "sma_cross": "sma_cross.SmaCrossStrategy",
    "mean_reversion": "mean_reversion.MeanReversionStrategy",
    # The protective-level producer: every entry declares a stop and a target,
    # so this is the strategy whose chart carries a bracket.
    "bracket_breakout": "bracket_breakout.BracketBreakoutStrategy",
}


def _build_strategy(name: str, instrument: Any, params: dict[str, Any]) -> Any:
    """Instantiate a known single-symbol strategy for the backtest endpoint.

    Only strategies whose constructor takes (strategy_id, instrument, **params)
    are exposed here — the same shape the backtest script builds.
    """
    path = _STRATEGY_FACTORIES.get(name)
    if path is None:
        raise HTTPException(
            status_code=422,
            detail=f"unknown strategy {name!r}; one of {sorted(_STRATEGY_FACTORIES)}",
        )
    module_name, class_name = path.split(".")
    import importlib

    try:
        cls = getattr(
            importlib.import_module(f"tradex_strategy.extensions.strategies.{module_name}"),
            class_name,
        )
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"strategy unavailable: {exc}") from exc

    known = _STRATEGY_PARAM_TYPES.keys()
    unknown = set(params) - set(known)
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown params: {sorted(unknown)}")
    clean_params: dict[str, Any] = {}
    allowed = set(_STRATEGY_DEFAULTS.get(name, {}).keys())
    for key, value in params.items():
        if key not in allowed:
            continue
        kind = _STRATEGY_PARAM_TYPES.get(key)
        if kind is None:
            continue
        clean_params[key] = int(value) if kind == "int" else float(value)
    try:
        return cls(strategy_id=f"{name}_chart", instrument=instrument, **clean_params)
    except TypeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _backtest_candles(instrument: Any, tf: Timeframe, start: datetime, end: datetime) -> list[Any]:
    """Load resampled candles for a backtest window from the datalake.

    Routes through :class:`~tradex_market_data.window_loader.MarketWindowLoader`
    so chart history, backtest and replay share one resolution path.
    """
    from tradex_market_data.window_loader import MarketWindowLoader

    return list(MarketWindowLoader().load(instrument, tf, start, end).candles)


#: Slippage the chart's backtests assume, in basis points of fill price. Stated
#: rather than defaulted to zero: a run that assumes free fills is not a
#: backtest, and an unfilled-by-assumption cost is indistinguishable from a
#: strategy that does not trade. 5 bps is the same knob ``--slippage-bps``
#: exposes on the canonical script, so the two can be compared directly.
_BACKTEST_SLIPPAGE_BPS = Decimal("5")


def _backtest_engine(body: dict[str, Any]) -> Any:
    """Build the ``BacktestEngine`` the chart backtest runs on.

    The three things that make a run's numbers mean something, all of them
    non-optional here (AC-2):

    - a ``RiskManager``, which is what makes the engine open a cash ledger
      anchored to ``initial_capital`` with its debit floor armed. Without one
      the floor is off, an order the capital cannot pay for books anyway, and
      the equity curve comes out flattering with no error anywhere.
    - a ``FeeCalculator``. Not decoration: the risk gate compares a BUY's
      *notional* against cash and the ledger floor compares *notional + fee*,
      so the fee is the only thing that can make the last affordable order
      unaffordable. A run with costs switched off cannot reach the floor at
      all.
    - a slippage model, for the reason above.

    Costs are on unconditionally. A caller must not be able to switch them off
    in a run whose output is read as evidence for promoting a strategy.
    """
    from tradex_execution.fees import FeeCalculator
    from tradex_execution.risk import RiskManager
    from tradex_execution.slippage import PercentageSlippageModel
    from tradex_replay.backtest import BacktestEngine

    # This is a research backtest surface, so it uses the RiskManager class
    # default (allow_short=True): the operator is exploring what a strategy
    # WOULD do, including shorting. The live/paper session is where the safety
    # default applies — RiskConfig.allow_short defaults to False there, and
    # boot() binds it. A UI backtest that allowed shorts the live session
    # rejects would be a research answer, not a live one; the operator aligns
    # the two by configuration, not by crippling the research surface.
    risk = RiskManager()
    # An unavailable balance is unknown, not zero: under the backtest the
    # provider is the run's own ledger, so this only bites if that binding is
    # ever lost, and losing it must stop BUYs rather than wave them through.
    risk.fail_closed_cash = True
    return BacktestEngine(
        initial_capital=body.get("initial_capital", int(DEFAULT_INITIAL_CAPITAL)),
        risk_manager=risk,
        fee_calculator=FeeCalculator(),
        slippage_model=PercentageSlippageModel(
            _BACKTEST_SLIPPAGE_BPS / Decimal("10000")
        ),
    )



def _ist_to_utc_seconds(ts: datetime) -> int:
    """IST tz-naive datalake timestamp -> UTC epoch seconds for the chart."""
    return int(ts.replace(tzinfo=IST_ZONE).timestamp())


@lru_cache(maxsize=4)
def _get_store(base_path: str) -> Any:
    """Cached ParquetStorage per base path (construction is cheap but not free)."""
    from tradex_market_data.parquet_storage import ParquetStorage

    return ParquetStorage(base_path)


class _HistoryCache:
    """Tiny TTL cache over serialized history responses."""

    def __init__(self, ttl_seconds: float = _CACHE_TTL_SECONDS, maxsize: int = 256) -> None:
        self._ttl = ttl_seconds
        self._maxsize = maxsize
        self._entries: dict[tuple, tuple[float, list[dict[str, Any]]]] = {}

    def get(self, key: tuple) -> list[dict[str, Any]] | None:
        hit = self._entries.get(key)
        if hit is None:
            return None
        stamp, payload = hit
        if time.monotonic() - stamp > self._ttl:
            del self._entries[key]
            return None
        return payload

    def put(self, key: tuple, payload: list[dict[str, Any]]) -> None:
        if len(self._entries) >= self._maxsize:
            # Arbitrary eviction is fine: entries are cheap and refilled on miss.
            self._entries.pop(next(iter(self._entries)))
        self._entries[key] = (time.monotonic(), payload)


_history_cache = _HistoryCache()


def _resolve_instrument(exchange: str, symbol: str) -> Equity:
    try:
        return Equity.of(exchange, symbol)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class WorkspacePutBody(BaseModel):
    """PUT /workspace body: opaque engine-state blob + optimistic revision."""
    data: dict[str, Any]
    revision: int | None = None
    force: bool = False


def _validate_layout_id(layout_id: str) -> None:
    if not layout_id or len(layout_id) > 256:
        raise HTTPException(status_code=422, detail="layout_id must be 1..256 chars")


def create_chart_router(
    session: Any | None, workspace_db_path: str | Path = ":memory:"
) -> APIRouter:
    """Build the /api/charts router bound to an optional TradingSession."""
    router = APIRouter(prefix="/api/charts", tags=["charts"])

    # ---------------------------------------------------------------- symbols

    @router.get("/symbols", dependencies=[Depends(require_auth)])
    async def list_symbols(
        q: str | None = None, source: str = "datalake", universe: str = "nifty50"
    ) -> dict:
        """Symbols available to chart, type-ahead ready.

        ``source=datalake`` (default): every symbol actually in the datalake —
        the chart can only render what exists, so this is the honest list.
        ``source=universe``: the universe CSV members (pre-datalake fallback).
        Search (``q``) is a case-insensitive substring filter over the result.
        """
        needle = (q or "").strip().upper()
        if source == "universe":
            from tradex_market_data.universe import load_universe

            members = [
                {"symbol": i.symbol, "exchange": str(i.exchange.value)}
                for i in load_universe(universe)
            ]
            results = [
                m for m in members if not needle or needle in m["symbol"].upper()
            ]
            return {"symbols": results[:200], "universe": universe, "source": "universe"}

        from tradex_market_data.paths import DATALAKE_ROOT

        store = _get_store(DATALAKE_ROOT)
        symbols = [
            s for s in store.symbols()
            if not needle or needle in s.upper()
        ]
        if not symbols:
            return {"symbols": [], "source": "datalake"}
        metadata = store.read(symbols=symbols)[["symbol", "exchange"]].dropna()
        results = [
            {"symbol": str(symbol), "exchange": str(exchange)}
            for symbol, exchange in metadata.drop_duplicates().itertuples(index=False, name=None)
            if str(exchange)
        ]
        return {"symbols": results[:500], "source": "datalake"}

    # ---------------------------------------------------------------- history

    @router.get("/history/{exchange}:{symbol}", dependencies=[Depends(require_auth)])
    async def get_history(
        exchange: str,
        symbol: str,
        interval: str = Query(default="5m"),
        from_utc: int | None = Query(default=None, alias="from"),
        to_utc: int | None = Query(default=None, alias="to"),
        limit: int = Query(default=5000, le=20000),
    ) -> dict:
        """Closed OHLCV bars for the chart, oldest first.

        Source precedence: parquet datalake first (offline, deterministic),
        broker history as fallback when the datalake has nothing in-window.
        Response carries ``last_closed_time`` so the live subscription knows
        where to take over — the server-side half of the never-cache-the-
        forming-bar rule.
        """
        tf = INTERVAL_TIMEFRAME.get(interval)
        if tf is None:
            raise HTTPException(
                status_code=422,
                detail=f"unsupported interval {interval!r}; one of {sorted(INTERVAL_TIMEFRAME)}",
            )
        instrument = _resolve_instrument(exchange, symbol)
        start, end = _window(from_utc, to_utc)

        cache_key = (exchange.upper(), symbol.upper(), interval, from_utc, to_utc, limit)
        cached = _history_cache.get(cache_key)
        if cached is not None:
            # Same field contract as the fresh path below, including
            # ``last_closed_time``: the frontend anchors its load window to the
            # newest closed bar, and a cache hit that omitted the field would
            # silently fall back to wall-clock (and load an empty intraday
            # window whenever the datalake lags). ``_serialize_series`` derives
            # it the same way, so a hit and a miss agree exactly.
            return {
                "symbol": symbol.upper(),
                "exchange": exchange.upper(),
                "interval": interval,
                "bars": cached,
                "cached": True,
                "last_closed_time": cached[-1]["time"] if cached else None,
            }

        bars, last_closed = _bars_from_datalake(instrument, tf, start, end, limit)
        source = "datalake"
        if not bars:
            bars, last_closed = _bars_from_broker(session, instrument, tf, start, end, limit)
            source = "broker" if bars else "none"

        payload = {
            "symbol": symbol.upper(),
            "exchange": exchange.upper(),
            "interval": interval,
            "source": source,
            "timeframe_resampled": tf.value,
            "last_closed_time": last_closed,
            "bars": bars,
        }
        if source != "none":
            _history_cache.put(cache_key, bars)
        return payload

    # ------------------------------------------------------------- indicators

    @router.get("/indicators", dependencies=[Depends(require_auth)])
    async def list_indicators() -> dict:
        """The whole indicator catalogue: ids, param schemas, plot shapes.

        The frontend builds its indicator menu entirely from this; adding an
        entry to the backend registry is the entire act of shipping an
        indicator to the UI.
        """
        from tradex_analytics.indicators import indicator_catalogue

        return {"indicators": indicator_catalogue()}

    @router.post("/indicators/compute", dependencies=[Depends(require_auth)])
    async def compute_indicator_endpoint(body: dict) -> dict:
        """Compute one registered indicator over the SAME bar window the
        chart holds, so returned points are index-aligned with its bars.

        Body: {exchange, symbol, interval, from?, to?, id, params?}.
        Points carry null (never NaN — json.dumps would emit a bare NaN and
        break JSON parsing) wherever the indicator has no value yet.
        """
        indicator_id = body.get("id")
        if not indicator_id or not isinstance(indicator_id, str):
            raise HTTPException(status_code=422, detail="body must include string 'id'")
        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise HTTPException(status_code=422, detail="'params' must be an object")
        tf = INTERVAL_TIMEFRAME.get(str(body.get("interval", "5m")))
        if tf is None:
            raise HTTPException(status_code=422, detail="unsupported interval")
        instrument = _resolve_instrument(
            str(body.get("exchange", "NSE")), str(body.get("symbol", ""))
        )
        # Resolve the id and its params BEFORE reading the lake. A malformed
        # request is malformed whatever the window holds — and the default
        # window (no from/to -> "now minus lookback") often covers no data at
        # all, so validating after the read meant an empty lake answered 200
        # with an empty plot and this 422 was unreachable for the default
        # request shape. Validation is separated from computation because a few
        # indicators (vortex) cannot compute over an empty candle list at all,
        # so "nothing to compute" must not mean "compute nothing". Same order
        # as /seasonality, which already validates first.
        try:
            from tradex_analytics.indicators import validate_indicator_request

            merged_params = validate_indicator_request(indicator_id, params)[1]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        start, end = _window(body.get("from"), body.get("to"))

        bars, _ = _bars_from_datalake(instrument, tf, start, end, limit=20000)
        if not bars:
            return {"id": indicator_id, "points": [], "meta": {"source": "none"}}

        # Compute over the exact candle list the serialized bars came from,
        # so plot indexes and bar times cannot drift apart.
        try:
            from tradex_analytics.indicators import compute_indicator

            values = compute_indicator(
                indicator_id, _candles_for_bars(instrument, bars), merged_params
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        plot_keys = list(values.keys())
        points: list[dict[str, Any]] = []
        n = max((len(v) for v in values.values()), default=0)
        # Indicators emit None-padded warmups of the input length; align each
        # plot's list tail against the bar list so short outputs still map.
        for i in range(n):
            point: dict[str, Any] = {}
            for key in plot_keys:
                series_values = values[key]
                offset = len(series_values) - n
                idx = i + offset
                v = series_values[idx] if 0 <= idx < len(series_values) else None
                point[key] = None if v is None else round(float(v), 10)
            points.append({"time": bars[i]["time"], **point})
        return {
            "id": indicator_id,
            "points": points,
            "meta": {"source": "datalake", "params": params},
        }

    @router.post("/transforms/{transform_id}", dependencies=[Depends(require_auth)])
    async def compute_transform_endpoint(transform_id: str, body: dict) -> dict:
        """Rebucket a bar window through one series transform (stateless).

        Body: {id, params?, bars: [{time, open, high, low, close, volume}]}.
        No datalake or session is touched — the chart posts its visible
        window and gets the transformed bars back verbatim.
        """
        from tradex_analytics.transforms import compute_transform

        bars = body.get("bars") or []
        if not isinstance(bars, list) or not bars:
            raise HTTPException(
                status_code=422, detail="transform requires a non-empty bars array"
            )
        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise HTTPException(status_code=422, detail="'params' must be an object")
        try:
            result = compute_transform(transform_id, bars, params)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": transform_id, "bars": result}

    @router.post("/profiles/{profile_id}", dependencies=[Depends(require_auth)])
    async def compute_profile_endpoint(profile_id: str, body: dict) -> dict:
        """Compute one profile study (or the seasonality table) over the bar
        window the chart holds (stateless).

        Body: {id, params?, bars: [{time, open, high, low, close, volume}]}.
        Seasonality dispatches to ``compute_seasonality`` (it returns a table,
        not a profile); every other id routes to ``compute_profile``. No
        datalake or session is touched — bars come from the request body.
        """
        from tradex_analytics.profiles import compute_profile
        from tradex_analytics.seasonality import compute_seasonality

        bars = body.get("bars") or []
        if not isinstance(bars, list) or not bars:
            raise HTTPException(
                status_code=422, detail="profile requires a non-empty bars array"
            )
        params = body.get("params") or {}
        try:
            if profile_id == "seasonality":
                result = compute_seasonality(bars, params)
            else:
                result = compute_profile(profile_id, bars, params)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": profile_id, "result": result}

    @router.post("/seasonality", dependencies=[Depends(require_auth)])
    async def compute_seasonality_endpoint(
        body: dict,
        startYear: int | None = Query(default=None),
        ignoredMonths: str | None = Query(default=None),
        cutoffPercent: float | None = Query(default=None),
        tablePosition: str | None = Query(default=None),
        tableWidth: float | None = Query(default=None),
        tableHeight: float | None = Query(default=None),
        showAvg: bool | None = Query(default=None),
        showStDev: bool | None = Query(default=None),
        showPos: bool | None = Query(default=None),
    ) -> dict:
        """Monthly seasonality heatmap over a datalake bar window.

        Body: {exchange, symbol, interval, from?, to?, params?}. ``params``
        accepts the engine's camelCase settings (startYear, cutoffPercent,
        ...) or snake_case; the query params above override the same keys
        when present. Bars come from the parquet datalake (offline, like
        the compute endpoint) already in the {time, close} shape
        ``compute_seasonality`` tabulates. An unknown symbol yields an
        empty table, never a 500.
        """
        from tradex_analytics.seasonality import (
            compute_seasonality,
            normalize_seasonality_params,
        )

        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="body must be an object")
        raw_params = body.get("params") or {}
        if not isinstance(raw_params, dict):
            raise HTTPException(status_code=422, detail="'params' must be an object")
        merged_raw = dict(raw_params)
        for _key, _val in (
            ("startYear", startYear),
            ("ignoredMonths", ignoredMonths),
            ("cutoffPercent", cutoffPercent),
            ("tablePosition", tablePosition),
            ("tableWidth", tableWidth),
            ("tableHeight", tableHeight),
            ("showAvg", showAvg),
            ("showStDev", showStDev),
            ("showPos", showPos),
        ):
            if _val is not None:
                merged_raw[_key] = _val
        try:
            params = normalize_seasonality_params(merged_raw)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        tf = INTERVAL_TIMEFRAME.get(str(body.get("interval", "D")))
        if tf is None:
            raise HTTPException(status_code=422, detail="unsupported interval")
        instrument = _resolve_instrument(
            str(body.get("exchange", "NSE")), str(body.get("symbol", ""))
        )
        from_raw, to_raw = body.get("from"), body.get("to")
        for _name, _t in (("from", from_raw), ("to", to_raw)):
            if _t is not None and (isinstance(_t, bool) or not isinstance(_t, (int, float))):
                raise HTTPException(
                    status_code=422, detail=f"{_name!r} must be UTC seconds"
                )
        start, end = _window(from_raw, to_raw)

        bars, _ = _bars_from_datalake(instrument, tf, start, end, limit=20000)
        if not bars:
            return {
                "table": {"rows": [], "options": {}},
                "meta": {"source": "none", "params": params},
            }
        try:
            table = compute_seasonality(bars, params)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"table": table, "meta": {"source": "datalake", "params": params}}

    # ------------------------------------------------------------------ trading

    @router.get("/book", dependencies=[Depends(require_auth)])
    async def get_book() -> dict:
        """Chart-shaped working book: orders + positions in the trade tier's
        vocabulary (openalgo-charts Order/Position). Read-only; writes go
        through the existing /orders endpoints so they traverse the same
        execution spine as every other client."""
        if session is None:
            return {"orders": [], "positions": []}
        try:
            raw_orders = session.engine.all_orders()
        except Exception as exc:  # noqa: BLE001 — degrade to empty, not 500
            raise HTTPException(status_code=502, detail=f"orderbook unavailable: {exc}") from exc
        try:
            raw_positions = session.engine.cache.all_positions()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"positions unavailable: {exc}") from exc

        orders = []
        for o in raw_orders or []:
            try:
                filled_q = getattr(o, "filled_quantity", None)
                filled = float(filled_q.value) if filled_q else 0.0
                trigger = (
                    float(o.trigger_price.value)
                    if getattr(o, "trigger_price", None) is not None
                    else None
                )
                orders.append({
                    "id": str(o.order_id),
                    "symbol": o.instrument.symbol,
                    "exchange": str(o.instrument.exchange.value),
                    "side": str(getattr(o.side, "value", o.side)),
                    "type": _map_order_type(o),
                    "qty": float(o.quantity.value),
                    "filledQty": filled,
                    "price": float(o.price.value) if o.price is not None else 0.0,
                    "triggerPrice": trigger,
                    "status": map_order_status(o.status),
                })
            except AttributeError as exc:
                raise HTTPException(status_code=500, detail=f"malformed order row: {exc}") from exc
        positions = [
            {
                "symbol": p.instrument.symbol,
                "exchange": str(p.instrument.exchange.value),
                "netQty": float(p.quantity.value),
                "avgPrice": float(p.avg_price.value),
            }
            for p in raw_positions or []
            if float(p.quantity.value) != 0.0
        ]
        return {"orders": orders, "positions": positions}

    # ---------------------------------------------------------------- account

    @router.get("/account", dependencies=[Depends(require_auth)])
    async def get_account() -> dict:
        """Account snapshot for the chart host's account panel.

        Returns balance, margin, net P&L and the positions the engine is
        currently holding. Paper mode reports the paper broker's cash + positions;
        live brokers report whatever ``get_account`` returns. No session means
        an empty snapshot (the panel shows its empty state).
        """
        if session is None:
            return {
                "account_id": "",
                "balance": "0",
                "margin": "0",
                "unrealized_pnl": "0",
                "positions": [],
            }
        try:
            acct = session.broker.get_account()
        except Exception as exc:  # noqa: BLE001 — degrade to empty, not 500
            raise HTTPException(status_code=502, detail=f"account unavailable: {exc}") from exc
        positions = []
        # Economic values come from the canonical projection so this route
        # cannot disagree with the numbers the parity suite pins across modes.
        # The wire shape stays numeric because the chart does arithmetic on it.
        # ``execution_projection`` returns ``dict[str, object]`` on purpose
        # (its payload is heterogeneous), so the ``"positions"`` entry needs
        # narrowing to the row shape before it can be indexed. The rows are
        # ``dict[str, Any]`` with an ``"instrument"`` key — exactly what the
        # comprehension reads. This replaces the previous ``type: ignore`` with
        # a real type.
        _position_rows = cast(
            list[dict[str, Any]],
            execution_projection([], session.engine.cache.all_positions())["positions"],
        )
        canonical = {row["instrument"]: row for row in _position_rows}
        for p in session.engine.cache.all_positions():
            try:
                if p.quantity.value == 0:
                    continue
                econ = canonical.get(str(p.instrument.instrument_id))
                positions.append({
                    "symbol": p.instrument.symbol,
                    "exchange": str(p.instrument.exchange.value),
                    "net_qty": float(p.quantity.value),
                    "avg_price": float(
                        econ["avg_price"] if econ else p.avg_price.value,
                    ),
                    "unrealized_pnl": float(
                        econ["unrealized_pnl"] if econ
                        else getattr(p, "unrealized_pnl", None) and
                        p.unrealized_pnl.amount or 0,
                    ),
                })
            except AttributeError:
                continue
        # A broker that reports no margin (or no balance) is missing data, not
        # zero money; the panel must still render rather than 500.
        balance = getattr(getattr(acct, "balance", None), "amount", None)
        margin = getattr(getattr(acct, "margin", None), "amount", None)
        return {
            "account_id": str(getattr(acct.account_id, "value", "")),
            "balance": str(balance if balance is not None else 0),
            "margin": str(margin if margin is not None else 0),
            "unrealized_pnl": str(sum(pos["unrealized_pnl"] for pos in positions)),
            "positions": positions,
        }

    # ---------------------------------------------------------------- strategies

    @router.get("/strategies", dependencies=[Depends(require_auth)])
    async def list_strategies() -> dict:
        """Auto-discovered strategies (extensions package) + scanners."""
        from tradex_strategy.extensions import all_scanners, all_strategies
        from tradex_strategy.extensions import scanners as scanners_pkg

        strategies = [
            {
                "id": type(s).__name__,
                "strategy_id": getattr(s, "strategy_id", ""),
                "version": getattr(s, "version", "1.0.0"),
            }
            for s in all_strategies
        ]
        # Backtestable strategies carry their param schema — name, type and
        # default — so the UI builds editable forms from the response instead of
        # hardcoding each strategy's shape.
        backtestable = [
            {
                "id": name,
                "params": [
                    {
                        "name": param,
                        "type": _STRATEGY_PARAM_TYPES[param],
                        "default": _STRATEGY_DEFAULTS[name][param],
                    }
                    for param in sorted(_STRATEGY_DEFAULTS[name])
                ],
            }
            for name in _STRATEGY_FACTORIES
        ]
        # ScannerDefinition is an anonymous frozen dataclass, so identity
        # lives in the declaring package's __all__ names, not the type.
        name_by_id = {
            id(obj): name
            for name in getattr(scanners_pkg, "__all__", ())
            if (obj := getattr(scanners_pkg, name, None)) is not None
        }
        scanners = [
            {
                "id": name_by_id.get(id(sc), type(sc).__name__),
                "universe_size": len(sc.universe),
                "conditions": [c.name for c in sc.conditions],
                "rank_by": sc.rank_by,
                "limit": sc.limit,
            }
            for sc in all_scanners
        ]
        return {
            "strategies": strategies,
            "scanners": scanners,
            "backtestable": backtestable,
        }

    # ----------------------------------------------------------------- safety

    @router.get("/safety", dependencies=[Depends(require_auth)])
    async def get_safety() -> dict:
        """The whole safety picture in one call (AC-17).

        Feed state and last-event age, unresolved UNKNOWN orders with their age
        and correlation id, reconciliation drift counted by *derived* severity
        with the time it was last checked, and per strategy its lifecycle
        state, promotion status, report id and any promotion failures.

        The handler is a shell: ``safety.build_safety_report`` derives every
        number, including the severity rule, so none of it can be tested only
        through HTTP. A session is optional and its absence is reported as
        absent — an unbooted server is itself a safety fact, not a 500.
        """
        if session is None:
            return build_safety_report(None).to_wire()
        return build_safety_report(session).to_wire()

    @router.post("/backtest", dependencies=[Depends(require_auth)])
    async def run_backtest(body: dict) -> dict:
        """Backtest one strategy over a datalake window via BacktestEngine.

        Body: {exchange, symbol, interval, from?, to?, strategy, params?,
        initial_capital?}. The engine is the SAME spine live uses
        (ExecutionEngine + PositionAccountant + fees); this endpoint only wires
        datalake bars into it. Trades come back as chart markers; the equity
        curve as an overlayable line.

        The engine is built by :func:`_backtest_engine`, so fees, slippage and
        the cash gate are always on. A run whose capital cannot pay for a fill
        raises out of the engine and is answered 422 with the typed error
        envelope — the alternative, which is what this route used to do, is a
        200 carrying a curve that flatters the strategy (AC-2).

        The engine grinds the window bar by bar, so ``run`` is handed to a
        worker thread: ``/ws/stream`` shares this event loop, and a backtest
        left on it stalls the live feed for its whole duration.
        """
        import asyncio
        from datetime import timedelta

        symbol = str(body.get("symbol", ""))
        exchange = str(body.get("exchange", "NSE"))
        if not symbol:
            raise HTTPException(status_code=422, detail="'symbol' required")
        # One normalization path (domain/intervals): the WS-style "1d" alias
        # folds onto the REST "D" the table keys on, so a Tier-2 fetch that
        # rides the same interval string the WebSocket layer uses does not
        # 422 here.
        raw_interval = str(body.get("interval", "D"))
        interval = normalize_interval(raw_interval)
        tf = INTERVAL_TIMEFRAME.get(interval)
        if tf is None:
            raise HTTPException(status_code=422, detail="unsupported interval")
        instrument = _resolve_instrument(exchange, symbol)
        start, end = _window(body.get("from"), body.get("to"))

        # Validate strategy before the empty-lake early return so unknown names
        # always 422 (scanner validates id first for the same reason).
        strategy = _build_strategy(
            str(body.get("strategy", "sma_cross")),
            instrument,
            body.get("params") or {},
        )

        candles = _backtest_candles(instrument, tf, start - timedelta(days=1), end)
        if not candles:
            return {"metrics": None, "message": f"no datalake data for {symbol} in window"}

        engine = _backtest_engine(body)
        try:
            result = await asyncio.to_thread(engine.run, strategy, candles)
        except ValueError as exc:
            # The run overran its capital. The engine raises rather than
            # absorbing it (AC-2); a request whose premise cannot be funded is
            # a bad request, not a server fault, so it is 422 with the same
            # message the operator would see in the engine's log.
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # Equity curve points ride bar timestamps where available; the curve
        # is per-event, so down-map by index onto the candle list.
        curve_times = [c.timestamp for c in candles]
        equity_points = []
        for i, value in enumerate(result.equity_curve):
            ts = curve_times[min(i, len(curve_times) - 1)]
            equity_points.append({"time": _ist_to_utc_seconds(ts), "value": float(value)})
        # Markers come from recorded FILLS (real prices/timestamps from the
        # execution pipeline), not strategy signals — an unfilled or rejected
        # signal has no price to draw. Rejections ride the same list flagged
        # so the chart can render them as error markers.
        ist = IST_ZONE

        def _fill_time(ts: datetime) -> int:
            naive = (
                ts.replace(tzinfo=None)
                if ts.tzinfo is None
                else ts.astimezone(ist).replace(tzinfo=None)
            )
            return _ist_to_utc_seconds(naive)

        trades = [
            {
                "time": _fill_time(f["time"]),
                "side": f["side"],
                "price": f["price"],
                "qty": f.get("qty"),
                "reason": f["reason"],
                "rejected": f["rejected"],
                # The protective legs the order carried, when the strategy
                # declared them. `null` is not "no protection" — it is "this
                # order declared none", and the chart draws only what is here
                # rather than inventing a level.
                "stop": f.get("stop"),
                "target": f.get("target"),
            }
            for f in result.fills
        ]
        return {
            "metrics": {
                "total_return": float(result.total_return),
                "sharpe": float(result.sharpe),
                "max_drawdown": float(result.max_drawdown),
                "num_trades": result.num_trades,
                "num_rejected": result.num_rejected,
                # Signals with no bar after the one they were decided on, so
                # the run could not fill them (AC-1). Surfaced so a reviewer
                # can see trades + unfilled + rejected accounts for the
                # signals rather than quietly not.
                "num_unfilled": result.num_unfilled,
                "total_fees": float(result.total_fees),
            },
            "equity_curve": equity_points,
            "trades": trades,
        }

    @router.post("/scanner/run", dependencies=[Depends(require_auth)])
    async def run_scanner(body: dict) -> dict:
        """Run one discovered scanner across its universe via ScannerEngine.

        Body: {id, window_days?}. The engine is the same one the strategy
        runtime uses; the market provider reads the datalake, so scanning is
        offline and deterministic (no broker calls). The scan is CPU/IO bound
        over every universe partition, so it runs in a worker thread — the
        event loop must stay free for bar streaming while it grinds.
        """
        import asyncio

        from tradex_market_data.market_provider import BulkPrefetchMarketProvider
        from tradex_strategy.core.scanner import ScannerEngine
        from tradex_strategy.extensions import scanners as scanners_pkg

        scanner_id = str(body.get("id", ""))
        definition = next(
            (
                obj
                for name in getattr(scanners_pkg, "__all__", ())
                if name == scanner_id
                and (obj := getattr(scanners_pkg, name, None)) is not None
            ),
            None,
        )
        if definition is None:
            raise HTTPException(
                status_code=404,
                detail=f"unknown scanner {scanner_id!r}; one of "
                f"{list(getattr(scanners_pkg, '__all__', ()))}",
            )
        try:
            window_days = int(body.get("window_days", 30))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="window_days must be an integer")
        if not 5 <= window_days <= 120:
            raise HTTPException(status_code=422, detail="window_days must be 5..120")
        # One multi-symbol read covers every universe partition in a single
        # pass; per-instrument history() then slices memory instead of
        # re-walking Hive partitions 500 times (~100 s -> seconds).
        now = datetime.now(UTC)
        symbols = list({getattr(i, "symbol") for i in definition.universe})
        market = await asyncio.to_thread(
            BulkPrefetchMarketProvider,
            symbols,
            now - timedelta(days=window_days),
            now,
        )
        engine = ScannerEngine(market, window_days=window_days)
        results = await asyncio.to_thread(engine.run, definition)
        return {
            "scanner": scanner_id,
            "window_days": window_days,
            "results": [
                {
                    "symbol": r.instrument.symbol,
                    "exchange": str(r.instrument.exchange.value),
                    "score": r.score,
                    "matched": r.matched_conditions,
                    "values": r.indicator_values,
                    "rank": r.rank,
                }
                for r in results
            ],
        }

    # ------------------------------------------------------------------ helpers

    def _bars_from_datalake(
        instrument: Any, tf: Timeframe, start: datetime, end: datetime, limit: int
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Datalake bars via the shared :class:`MarketWindowLoader`.

        Candle construction and resampling stay single-sourced in
        ``ParquetMarketProvider``; this endpoint adds only the IST→UTC edge
        conversion for the chart wire format.
        """
        from tradex_market_data.window_loader import MarketWindowLoader, series_from_window

        window = MarketWindowLoader(
            metrics=getattr(session, "metrics", None),
        ).load(instrument, tf, start, end)
        return _serialize_series(series_from_window(window), limit)

    def _bars_from_broker(
        session: Any | None,
        instrument: Any,
        tf: Timeframe,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Broker fallback. Best-effort: no session or provider failure -> empty."""
        if session is None:
            return [], None
        try:
            series = session.broker.history(instrument, tf, start, end)
        except Exception:  # noqa: BLE001 — fallback must degrade, not error the chart
            return [], None
        return _serialize_series(series, limit)

    # ------------------------------------------------------------- workspace

    from tradex_interfaces.workspace_store import WorkspaceStore

    workspace = WorkspaceStore(workspace_db_path)

    # The workspace surface is deliberately unauthenticated (no `require_auth`
    # on any of its four routes, reads or writes). A layout blob is local
    # per-workspace state, not user or account data, and the server is
    # loopback-only by construction: `serve` defaults to 127.0.0.1 and live mode
    # is refused on any other bind (`_require_live_bind_loopback`). `require_auth`
    # is additionally fail-open in paper/dev (no API key configured -> allowed),
    # so gating this surface would buy nothing in the only mode it runs in.
    # Kept consistent on purpose: an earlier revision authed the reads and not
    # the writes, which read as an oversight rather than a decision.
    @router.get("/workspace")
    async def list_workspaces() -> list[dict[str, Any]]:
        """Workspace metadata (no blobs): layout ids with revision + updated_at."""
        return workspace.list()

    @router.get("/workspace/{layout_id}")
    async def get_workspace(layout_id: str) -> dict[str, Any]:
        _validate_layout_id(layout_id)
        stored = workspace.get(layout_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"no workspace {layout_id!r}")
        return stored

    @router.put("/workspace/{layout_id}")
    async def put_workspace(layout_id: str, body: WorkspacePutBody) -> dict[str, Any]:
        _validate_layout_id(layout_id)
        new_revision, conflicted = workspace.put(
            layout_id, body.data, body.revision, bool(body.force)
        )
        if conflicted:
            raise HTTPException(
                status_code=409,
                detail={"message": "stale revision", "stored_revision": new_revision},
            )
        return {
            "layout_id": layout_id,
            "revision": new_revision,
            "updated_at": workspace.get(layout_id)["updated_at"],  # type: ignore[index]
        }

    @router.delete("/workspace/{layout_id}", status_code=204)
    async def delete_workspace(layout_id: str) -> None:
        _validate_layout_id(layout_id)
        workspace.delete(layout_id)

    return router


# ---------------------------------------------------------------------------
# Serialization helpers (module level: shared by Task 3's compute endpoint)
# ---------------------------------------------------------------------------


def _window(from_utc: int | None, to_utc: int | None) -> tuple[datetime, datetime]:
    """Chart UTC-seconds window -> naive datetimes (interpreted as IST)."""
    ist = IST_ZONE
    if to_utc is not None:
        end = datetime.fromtimestamp(to_utc, tz=UTC).astimezone(ist).replace(tzinfo=None)
    else:
        end = datetime.now(ist).replace(tzinfo=None)
    if from_utc is not None:
        start = datetime.fromtimestamp(from_utc, tz=UTC).astimezone(ist).replace(tzinfo=None)
    else:
        start = end - _DEFAULT_LOOKBACK
    return start, end


def _candles_for_bars(instrument: Any, bars: list[dict[str, Any]]) -> list[Any]:
    """Rebuild domain Candles from serialized bars (compute path).

    The indicator functions read ``.ohlc.high/.low/.close`` and ``.volume``
    off domain Candles; this rebuilds them from the exact dicts the response
    serves so compute and chart data are index-aligned by construction.
    """
    from tradex_brokers.common.market_builders import make_candle
    from tradex_domain.enums import Timeframe

    tf = Timeframe.M1  # timeframe label only; indicators never bucket on it
    return [
        make_candle(
            instrument,
            tf,
            open=b["open"],
            high=b["high"],
            low=b["low"],
            close=b["close"],
            volume=int(b.get("volume", 0) or 0),
            timestamp=datetime.fromtimestamp(b["time"], tz=UTC),
        )
        for b in bars
    ]


def _serialize_series(series: Any, limit: int) -> tuple[list[dict[str, Any]], int | None]:
    """HistoricalSeries -> chart Bar dicts + last-closed UTC second.

    Bars arrive oldest-first from resample; truncation keeps the TAIL (the
    most recent ``limit`` bars), which is what a chart wants.
    """
    candles = series.candles[-limit:] if len(series.candles) > limit else series.candles
    bars = [
        {
            "time": _ist_to_utc_seconds(c.timestamp),
            "open": float(c.ohlc.open.value),
            "high": float(c.ohlc.high.value),
            "low": float(c.ohlc.low.value),
            "close": float(c.ohlc.close.value),
            "volume": float(c.volume.value),
        }
        for c in candles
    ]
    last_closed = bars[-1]["time"] if bars else None
    return bars, last_closed  # type: ignore[return-value]
