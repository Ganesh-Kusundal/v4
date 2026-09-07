"""Stdio MCP server exposing the DuckDB analytics service.

Run standalone (any cwd; the data path is resolved walking up from this
file's location):

    python -m duck_analytics.mcp.server

Tools:
    query            raw read-only SQL against ohlcv/ohlcv_raw views
    schema           column list for a view
    list_symbols     distinct symbols in the lake
    date_range       min/max timestamp per symbol
    resample         canonical OHLCV aggregation to a higher timeframe
    scan_technical   SMA cross / RSI screen
    scan_volume      volume-spike screen
    scan_gap_vol     overnight gap / range screen
    breadth          % above N-DMA + advances/declines per day
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from duck_analytics.catalog import DuckDBCatalog, default_config_for
from duck_analytics.patterns import find_similar
from duck_analytics.query import QueryNotAllowedError, QueryService
from duck_analytics.resample import TF_TO_INTERVAL, resample_sql
from duck_analytics.rs_score import rs_score_sql
from duck_analytics.scanners import breadth, scan_screener

_REPO_HINT = Path(__file__).resolve().parents[5]  # .../v4

_cfg = default_config_for(_REPO_HINT)
_catalog = DuckDBCatalog(_cfg)
_service = QueryService(_catalog, _cfg)

mcp = FastMCP("duck-analytics", instructions=(
    "Read-only analytics over the TradeX OHLCV parquet datalake. "
    "Views: ohlcv (session-stripped 09:15-15:30 IST) and ohlcv_raw. "
    "Scanner tools require as_of and never see bars after it."
))


def _payload(res: Any) -> str:
    return json.dumps(res, default=str, ensure_ascii=False)


def _rows_payload(result) -> dict[str, Any]:
    return {
        "columns": result.columns,
        "rows": [list(r) for r in result.rows],
        "row_count": result.row_count,
        "total_row_count": result.total_row_count,
        "truncated": result.truncated,
        "elapsed_ms": round(result.elapsed_ms, 1),
        "point_in_time_safe": result.point_in_time_safe,
    }


@mcp.tool()
def query(sql: str, limit: int | None = None) -> str:
    """Execute read-only SELECT/WITH SQL against views `ohlcv` or `ohlcv_raw`.

    Named params are NOT supported here — inline literals only (validated).
    A server-side LIMIT is enforced (default 1000, max 50000). Mutations,
    ATTACH/COPY/INSTALL and multiple statements are rejected.
    """
    try:
        return _payload(_rows_payload(_service.execute(sql, None, limit=limit)))
    except QueryNotAllowedError as e:
        return _payload({"error": str(e)})


@mcp.tool()
def schema(view: str = "ohlcv") -> str:
    """Column names/types for `ohlcv` or `ohlcv_raw`."""
    try:
        return _payload({"view": view, "columns": _catalog.schema_of(view)})
    except ValueError as e:
        return _payload({"error": str(e)})


@mcp.tool()
def list_symbols() -> str:
    """All symbols present in the datalake, sorted."""
    return _payload({"symbols": _catalog.list_symbols()})


@mcp.tool()
def date_range(symbol: str) -> str:
    """Min/max stored timestamp for one symbol."""
    dr = _catalog.date_range(symbol)
    if dr is None:
        return _payload({"symbol": symbol, "error": "no data"})
    return _payload({"symbol": symbol, "start": str(dr[0]), "end": str(dr[1])})


@mcp.tool()
def resample(timeframe: str, start: str, end: str, symbols_json: str | None = None) -> str:
    """Canonical OHLCV aggregation from M1 bars.

    timeframe: one of 1m/5m/15m/30m/1h/1d. start/end inclusive 'YYYY-MM-DD[ HH:MM:SS]'.
    symbols_json: optional JSON array of symbols; omit for all.
    """
    if timeframe not in TF_TO_INTERVAL:
        return _payload({"error": f"timeframe must be one of {sorted(TF_TO_INTERVAL)}"})
    symbols = json.loads(symbols_json) if symbols_json else None
    sql = resample_sql(timeframe, start, end, symbols)
    return _payload(_rows_payload(_service.execute(sql, None)))


@mcp.tool()
def run_screener(
    as_of: str,
    gap_min_pct: float = 0.5,
    vol_multiple: float = 3.0,
    pre30_min_pct: float = 0.3,
    adx_min: float = 20.0,
    atr_min_pct: float | None = 1.2,
    strict: bool = False,
) -> str:
    """THE top-gainer screener at the 09:45 decision point (single tool).

    Walk-forward tuned with 14-day rel-volume (May-Jul train, Aug holdout):
    gap >= 0.5% + vol >= 3x(14d) + drive >= 0.3% + RS>med + ADX>=20 rising
    + ATR% >= 1.2% + exhaustion <=3% + open>=100.
    Holdout: P(top10)=18.2% vs 2.0% baseline, +0.32%/signal at ~2.2/day.
    Pass atr_min_pct=None to disable the ATR floor; strict=True adds
    price acceptance (close_pos45 >=0.9) + OBV confirmation (strict mean
    +0.88%/signal at 0.5/day).
    """
    q = scan_screener(
        as_of,
        gap_min_pct=gap_min_pct,
        vol_multiple=vol_multiple,
        pre30_min_pct=pre30_min_pct,
        adx_min=adx_min,
        atr_min_pct=atr_min_pct,
        strict=strict,
    )
    out = _rows_payload(
        _service.execute(q.sql, None, require_complete=True)
    )
    out["scan"] = q.description
    out["point_in_time_safe"] = True
    return _payload(out)


@mcp.tool()
def run_breadth(as_of: str, dma: int = 20, timeframe: str = "1d") -> str:
    """Per-day market breadth: % of symbols above N-bar DMA, advances/declines."""
    q = breadth(as_of, dma=dma, timeframe=timeframe)
    out = _rows_payload(
        _service.execute(q.sql, None, require_complete=True)
    )
    out["scan"] = q.description
    out["point_in_time_safe"] = True
    return _payload(out)


@mcp.tool()
def run_rs_score(
    as_of: str,
    days: int = 20,
    timeframe: str = "15m",
    bars_per_day: int | None = None,
) -> str:
    """Relative Strength score per symbol at a point in time.

    Weighted MA-structure momentum normalized by ATR; exponential recency
    weights (weight at midpoint is exactly half the newest). timeframe:
    1m/5m/15m/30m/1h. bars_per_day defaults to 25 for NSE intraday; pass 96
    for crypto-style 24h markets.
    """
    if timeframe not in TF_TO_INTERVAL or TF_TO_INTERVAL[timeframe] is None:
        return _payload({"error": "timeframe must be one of 5m/15m/30m/1h"})
    bpd = bars_per_day if bars_per_day is not None else 25
    sql = rs_score_sql(end=as_of, days=days, bars_per_day=bpd,
                       interval=TF_TO_INTERVAL[timeframe])
    out = _rows_payload(_service.execute(sql, None, require_complete=True))
    out["point_in_time_safe"] = True
    out["params"] = {"days": days, "timeframe": timeframe, "bars_per_day": bpd}
    return _payload(out)


@mcp.tool()
def find_similar_patterns(
    symbol: str,
    as_of: str,
    window_days: int = 20,
    horizon_days: int = 5,
    top_k: int = 10,
    exclude_overlap_days: int = 5,
) -> str:
    """DTW nearest-neighbors of a symbol's recent close pattern across history.

    Returns top_k historical (symbol, end-date) windows most similar by
    shape (z-normalized, warp-tolerant), each with its forward return over
    horizon_days. Read the forward-return distribution as "what happened
    next" evidence after similar setups.
    """
    try:
        res = find_similar(
            _service,
            query_symbol=symbol,
            end=as_of,
            window=window_days,
            horizon=horizon_days,
            top_k=top_k,
            exclude_overlap_days=exclude_overlap_days,
        )
    except ValueError as e:
        return _payload({"error": str(e)})
    return _payload({
        "query_symbol": res.query_symbol,
        "window_days": res.window,
        "horizon_days": res.horizon,
        "baseline_median_fwd_pct": round(res.baseline_median_fwd, 3),
        "matches": res.matches.to_dict(orient="records"),
    })


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
