"""Pure, JSON-safe research tools over the deterministic core.

These are the functions an MCP/agent layer calls. They take domain objects (or
JSON-friendly primitives) and return JSON-safe dicts; they never import an LLM or
a broker. The deterministic core (``BacktestEngine``) stays the single source of
truth — these tools only serialize and adapt.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from tradex_replay.backtest import BacktestEngine

#: Tool specs advertised over MCP (``tools/list``). Each tool is JSON-in/JSON-out.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "backtest",
        "description": (
            "Run a deterministic backtest of a registered strategy over a "
            "datalake window. Returns metrics, the equity curve, and drawdown "
            "episodes. Costs (fees + slippage) and the cash floor are always on."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "strategy": {"type": "string", "description": "Registered strategy id."},
                "symbol": {"type": "string"},
                "exchange": {"type": "string", "default": "NSE"},
                "interval": {"type": "string", "default": "1D"},
                "start": {"type": "string", "description": "ISO date/datetime."},
                "end": {"type": "string", "description": "ISO date/datetime."},
                "initial_capital": {"type": "number", "default": 100000},
                "params": {
                    "type": "object",
                    "description": "Strategy constructor params (e.g. fast/slow).",
                },
            },
            "required": ["strategy", "symbol", "start", "end"],
        },
    },
    {
        "name": "list_strategies",
        "description": "List registered strategy ids.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def bars_to_records(candles: list[Any]) -> list[dict[str, Any]]:
    """Serialize candles to JSON-safe records (ISO timestamp, float OHLCV)."""
    records: list[dict[str, Any]] = []
    for candle in candles:
        ts = candle.timestamp
        records.append(
            {
                "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "open": float(candle.ohlc.open.value),
                "high": float(candle.ohlc.high.value),
                "low": float(candle.ohlc.low.value),
                "close": float(candle.ohlc.close.value),
                "volume": float(candle.volume.value),
            }
        )
    return records


def result_to_dict(result: Any) -> dict[str, Any]:
    """Serialize a ``BacktestResult`` to a JSON-safe dict (metrics + episodes)."""
    return {
        "metrics": {
            "total_return": result.total_return,
            "sharpe": result.sharpe,
            "max_drawdown": result.max_drawdown,
            "sortino": result.sortino,
            "calmar": result.calmar,
            "volatility": result.volatility,
            "win_rate": result.win_rate,
            "profit_factor": result.profit_factor,
            "expectancy": result.expectancy,
            "exposure": result.exposure,
            "ulcer_index": result.ulcer_index,
            "pain_index": result.pain_index,
            "num_trades": result.num_trades,
            "num_rejected": result.num_rejected,
            "num_unfilled": result.num_unfilled,
            "total_fees": result.total_fees,
        },
        "equity_curve": list(result.equity_curve),
        "drawdown_episodes": [dict(ep) for ep in result.drawdown_episodes],
    }


def run_backtest(
    strategy: Any,
    candles: list[Any],
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run ``BacktestEngine`` over ``(strategy, candles)`` and serialize the result.

    ``config`` is passed straight to ``BacktestEngine.__init__`` (e.g.
    ``initial_capital``, ``slippage_model``, ``partial_fills``). This is the
    single core tool every higher-level surface (HTTP, MCP, agent) reuses.
    """
    engine = BacktestEngine(**dict(config or {}))
    result = engine.run(strategy, list(candles))
    return result_to_dict(result)


def _load_candles(instrument: Any, timeframe: Any, start: datetime, end: datetime) -> list[Any]:
    from tradex_market_data.window_loader import MarketWindowLoader

    return list(MarketWindowLoader().load(instrument, timeframe, start, end).candles)


def backtest(
    *,
    strategy: str,
    symbol: str,
    exchange: str = "NSE",
    interval: str = "1D",
    start: str,
    end: str,
    params: dict[str, Any] | None = None,
    initial_capital: float = 100000,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """MCP tool: backtest a registered strategy over a datalake window.

    Resolves the instrument, timeframe, candles and a *per-instrument* strategy
    instance (from the strategy definition, not a pre-bound instance), then runs
    the same deterministic core as ``run_backtest``. Raises ``ValueError`` for an
    unknown strategy or interval, and ``FileNotFoundError`` when the lake has no
    data.
    """
    from datetime import datetime as _dt

    from tradex_domain.enums import Timeframe
    from tradex_domain.instruments import Equity
    from tradex_domain.timeframe import INTERVAL_TO_TIMEFRAME
    from tradex_strategy.extensions import all_definitions

    tf_raw = INTERVAL_TO_TIMEFRAME.get(interval, interval)
    timeframe = Timeframe(tf_raw)
    instrument = Equity.of(exchange, symbol)

    definition = next(
        (d for d in all_definitions if d.spec.id == strategy), None
    )
    if definition is None:
        raise ValueError(f"unknown strategy: {strategy!r}")
    strategy_obj = definition.instantiate(instrument, **(params or {}))

    start_dt = _dt.fromisoformat(str(start).replace("Z", "+00:00"))
    end_dt = _dt.fromisoformat(str(end).replace("Z", "+00:00"))
    candles = _load_candles(instrument, timeframe, start_dt, end_dt)
    if not candles:
        raise FileNotFoundError(
            f"no datalake bars for {exchange}:{symbol} {interval} in window"
        )

    merged = dict(config or {})
    merged.setdefault("initial_capital", initial_capital)
    return run_backtest(strategy_obj, candles, config=merged)


def list_strategies() -> list[str]:
    """MCP tool: list registered strategy-definition ids."""
    from tradex_strategy.extensions import all_definitions

    return sorted({d.spec.id for d in all_definitions})
