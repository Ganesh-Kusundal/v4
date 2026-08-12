#!/usr/bin/env python3
"""Multi-symbol backtest off the parquet datalake (fully offline).

Loads OHLCV via ``ParquetBacktestLoader`` and runs a strategy through
``BacktestEngine`` per symbol, printing a metrics table.

Usage::

    python trading/scripts/backtest_datalake.py \\
        --universe nifty100 --timeframe 1d --months 2 \\
        --strategy sma_cross --fast 5 --slow 20 \\
        --max-workers 4 --limit 10 --fees

    # portfolio mode — one MultiSymbolSmaCross instance, one engine pass:
    python trading/scripts/backtest_datalake.py \\
        --universe nifty200 --strategy multi_symbol --fast 5 --slow 20

    # walk-forward OOS validation (grid-search on train, score on test):
    python trading/scripts/backtest_datalake.py \\
        --universe nifty50 --strategy sma_cross --optimize --walk-forward

Design
------
Single-instrument strategies (``sma_cross``, ``mean_reversion``) are
stateful, so the script runs one instance per symbol and aggregates
per-symbol ``BacktestResult``. ``multi_symbol`` runs the portfolio-style
``MultiSymbolSmaCross`` once over the whole universe in a single engine
pass. Data is loaded once — in parallel across symbols when
``--max-workers > 1`` — and grouped by instrument, so there are no
per-symbol re-reads.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

# Ensure project packages are importable
ROOT = Path(__file__).resolve().parent.parent.parent  # repo root
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))

from tradex_domain import Equity, Timeframe  # noqa: E402

from tradex_trading.datalake.backtest_loader import ParquetBacktestLoader  # noqa: E402
from tradex_trading.execution.fees import FeeCalculator  # noqa: E402
from tradex_trading.execution.slippage import PercentageSlippageModel  # noqa: E402
from tradex_trading.replay.backtest import BacktestEngine  # noqa: E402
from tradex_trading.runtime.calendar import NSETradingCalendar  # noqa: E402

log = logging.getLogger("tradex.scripts.backtest_datalake")


def _make_strategy(name: str, symbol: str, args, **overrides) -> object:
    """Instantiate the requested strategy for one NSE symbol.

    ``**overrides`` replaces specific CLI params (used by ``--optimize``
    to inject grid-search candidate values).
    """
    inst = Equity.of("NSE", symbol)
    if name == "sma_cross":
        from tradex_trading.strategy.extensions.strategies.sma_cross import (
            SmaCrossStrategy,
        )
        return SmaCrossStrategy(
            strategy_id=f"{name}_{symbol}",
            instrument=inst,
            fast=overrides.get("fast", args.fast),
            slow=overrides.get("slow", args.slow),
        )
    if name == "mean_reversion":
        from tradex_trading.strategy.extensions.strategies.mean_reversion import (
            MeanReversionStrategy,
        )
        return MeanReversionStrategy(
            strategy_id=f"{name}_{symbol}",
            instrument=inst,
            period=overrides.get("period", args.period),
            overbought=args.overbought,
            oversold=args.oversold,
        )
    raise ValueError(f"unknown strategy: {name}")


def _make_portfolio_strategy(args, **overrides) -> object:
    """Instantiate the portfolio strategy (one pass over the whole universe)."""
    from tradex_trading.strategy.extensions.strategies.multi_symbol_sma_cross import (
        MultiSymbolSmaCross,
    )
    return MultiSymbolSmaCross(
        fast=overrides.get("fast", args.fast),
        slow=overrides.get("slow", args.slow),
    )


def _param_grid(args) -> dict:
    """Grid-search candidate values for the selected strategy."""
    if args.strategy == "mean_reversion":
        return {"period": [7, 14, 21]}
    return {"fast": [3, 5, 10], "slow": [15, 20, 30]}


def _make_engine(args) -> BacktestEngine:
    """Build a BacktestEngine honoring --fees/--slippage-bps."""
    kwargs = {}
    if args.fees:
        kwargs["fee_calculator"] = FeeCalculator()
    if args.slippage_bps > 0:
        kwargs["slippage_model"] = PercentageSlippageModel(
            Decimal(str(args.slippage_bps)) / Decimal("10000")
        )
    return BacktestEngine(**kwargs)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Backtest strategies against the parquet datalake"
    )
    p.add_argument("--universe", default="nifty100",
                   choices=["nifty50", "nifty100", "nifty200", "nifty500"],
                   help="Nifty universe to backtest (default: nifty100)")
    p.add_argument("--symbols", default=None,
                   help="Comma-separated symbol override (e.g. RELIANCE,TCS; "
                        "takes precedence over --universe)")
    p.add_argument("--timeframe", default="1d",
                   choices=["1m", "5m", "15m", "30m", "1h", "1d"],
                   help="Bar timeframe; the M1 datalake is resampled (default: 1d)")
    p.add_argument("--months", type=int, default=3,
                   help="Trailing months to backtest (default: 3)")
    p.add_argument("--strategy", default="sma_cross",
                   choices=["sma_cross", "mean_reversion", "multi_symbol"],
                   help="Strategy to run: per-symbol sma_cross/mean_reversion, "
                        "or multi_symbol (one portfolio pass over the universe)"
                        " (default: sma_cross)")
    p.add_argument("--fast", type=int, default=5, help="SMA fast period")
    p.add_argument("--slow", type=int, default=20, help="SMA slow period")
    p.add_argument("--period", type=int, default=14, help="RSI period")
    p.add_argument("--overbought", type=float, default=70.0, help="RSI overbought level")
    p.add_argument("--oversold", type=float, default=30.0, help="RSI oversold level")
    p.add_argument("--max-workers", type=int, default=4,
                   help="Parallel symbol reads (default: 4)")
    p.add_argument("--limit", type=int, default=0,
                   help="Cap symbols backtested (0 = no limit)")
    p.add_argument("--fees", action="store_true",
                   help="Apply the equity FeeCalculator to fills")
    p.add_argument("--slippage-bps", type=float, default=0.0,
                   help="Percentage slippage in bps (0 = none)")
    p.add_argument("--optimize", action="store_true",
                   help="Grid-search strategy params per symbol (best params printed)")
    p.add_argument("--optimize-workers", type=int, default=1,
                   help="Parallel grid combinations per symbol (default: 1)")
    p.add_argument("--walk-forward", action="store_true",
                   help="With --optimize: grid-search on train windows and report "
                        "out-of-sample return via run_walk_forward_by_date (test "
                        "window should exceed the strategy warmup, e.g. > slow "
                        "for sma_cross)")
    p.add_argument("--train-window", type=int, default=30,
                   help="Walk-forward train window in calendar days (default: 30)")
    p.add_argument("--test-window", type=int, default=15,
                   help="Walk-forward test window in calendar days (default: 15)")
    p.add_argument("--data-root", default=None,
                   help="Parquet base path (default: <repo>/data)")
    p.add_argument("--strict-gaps", action="store_true",
                   help="Fail the run when a symbol is missing interior trading "
                        "days (default: warn and continue)")
    p.add_argument("--holidays", default=None,
                   help="Comma-separated ISO dates of NSE exchange holidays "
                        "(e.g. 2026-08-15,2026-10-02). Pass these so --strict-gaps "
                        "does not mistake holidays for missing data")
    p.add_argument("--delisted", default=None,
                   help="Comma-separated symbols to exclude from the universe "
                        "(survivorship-bias protection, e.g. --delisted XYZ,ABC)")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    if args.strategy in ("sma_cross", "multi_symbol") and args.fast >= args.slow:
        log.error("--fast must be smaller than --slow for %s", args.strategy)
        return 1
    if args.strategy == "mean_reversion" and args.oversold >= args.overbought:
        log.error("--oversold must be below --overbought for mean_reversion")
        return 1
    if args.walk_forward and not args.optimize:
        log.error("--walk-forward requires --optimize")
        return 1

    from datetime import date as _date

    holiday_dates = None
    if args.holidays:
        holiday_dates = {
            _date.fromisoformat(d.strip())
            for d in args.holidays.split(",")
            if d.strip()
        }
    delisted_symbols = None
    if args.delisted:
        delisted_symbols = {
            sym.strip().upper()
            for sym in args.delisted.split(",")
            if sym.strip()
        }
    loader = ParquetBacktestLoader(
        base_path=args.data_root or str(ROOT / "data"),
        gap_strict=args.strict_gaps,
        calendar=NSETradingCalendar(holidays=holiday_dates) if holiday_dates else None,
        delisted=delisted_symbols,
    )
    start = datetime.now() - timedelta(days=args.months * 30)
    end = datetime.now()

    instruments = None
    if args.symbols:
        instruments = [
            Equity.of("NSE", sym.strip())
            for sym in args.symbols.split(",")
            if sym.strip()
        ]
    elif args.limit > 0:
        # Cap the universe BEFORE loading, so --limit doesn't read every
        # symbol just to backtest a handful.
        from tradex_trading.datalake.universe import load_universe
        instruments = load_universe(args.universe)[:args.limit]
    source = args.universe if instruments is None else f"{len(instruments)} symbols"
    log.info("Loading %s (%s, %s) from datalake...",
             source, args.timeframe, args.months)

    candles = loader.load(
        instruments=instruments,
        universe=args.universe if instruments is None else None,
        timeframe=Timeframe(args.timeframe),
        start=start,
        end=end,
        max_workers=args.max_workers,
    )

    by_symbol: dict[str, list] = defaultdict(list)
    for candle in candles:
        by_symbol[candle.instrument.symbol].append(candle)
    symbols = sorted(by_symbol)
    if args.limit > 0:
        symbols = symbols[:args.limit]

    if not symbols:
        log.warning("No data loaded — is the datalake backfilled "
                    "(trading/scripts/backfill_parquet.py)?")
        return 1

    engine = _make_engine(args)
    log.info("Running %s over %d symbols (%d bars loaded)...",
             args.strategy, len(symbols), len(candles))

    if args.strategy == "multi_symbol":
        return _run_portfolio(args, engine, candles)

    if args.optimize:
        return _run_optimize(args, engine, by_symbol, symbols)

    rows = []
    for symbol in symbols:
        strategy = _make_strategy(args.strategy, symbol, args)
        result = engine.run(strategy, by_symbol[symbol])
        rows.append((
            symbol,
            len(by_symbol[symbol]),
            result.num_trades,
            result.total_return,
            result.sharpe,
            result.max_drawdown,
            result.total_fees,
        ))

    print(f"{'symbol':<12} {'bars':>6} {'trades':>7} {'return%':>9} "
          f"{'sharpe':>7} {'maxDD%':>8} {'fees':>10}")
    for symbol, bars, trades, ret, sharpe, dd, fees in rows:
        print(f"{symbol:<12} {bars:>6} {trades:>7} {ret * 100:>9.2f} "
              f"{sharpe:>7.2f} {dd * 100:>8.2f} {fees:>10.2f}")

    winners = [r for r in rows if r[3] > 0]
    log.info("Summary: %d/%d symbols with positive return",
             len(winners), len(rows))
    return 0


def _run_optimize(args, engine: BacktestEngine, by_symbol: dict, symbols: list[str]) -> int:
    """Grid-search strategy params per symbol, printing each best result.

    With ``--walk-forward``, delegates to :func:`_run_walk_forward` for
    out-of-sample validation instead.

    ``grid_search`` skips any combination whose backtest raises, so the grid
    can be naive (e.g. ``fast >= slow`` combos are dropped) — but a systemic
    failure surfaces as ``<no valid combos>`` for that symbol rather than an
    abort.
    """
    from tradex_trading.replay.optimization import grid_search

    if args.walk_forward:
        return _run_walk_forward(args, engine, by_symbol, symbols)

    param_grid = _param_grid(args)
    log.info("Optimizing %s over %s", args.strategy, param_grid)

    def run_fn(params, symbol: str, candles: list) -> object:
        strategy = _make_strategy(args.strategy, symbol, args, **params)
        return engine.run(strategy, candles)

    print(f"{'symbol':<12} {'best-params':<26} {'return%':>9} {'trades':>7} {'sharpe':>7}")
    winners = 0
    for symbol in symbols:
        search = grid_search(
            param_grid,
            lambda params, s=symbol: run_fn(params, s, by_symbol[s]),
            max_workers=args.optimize_workers,
        )
        best = search.best
        if best is None:
            print(f"{symbol:<12} {'<no valid combos>':<26}")
            continue
        params_str = ",".join(f"{k}={v}" for k, v in best.params.items())
        print(f"{symbol:<12} {params_str:<26} {best.score * 100:>9.2f} "
              f"{best.result.num_trades:>7} {best.result.sharpe:>7.2f}")
        if best.score > 0:
            winners += 1

    log.info("Summary: %d/%d symbols with positive best-param return",
             winners, len(symbols))
    return 0


def _run_walk_forward(
    args, engine: BacktestEngine, by_symbol: dict, symbols: list[str],
) -> int:
    """Walk-forward OOS validation per symbol.

    Each step grid-searches on the train window (calendar days) and scores the
    best params on the test window; the aggregate OOS return is the
    out-of-sample verdict. Symbols with fewer distinct dates than
    ``train + test`` days are skipped.
    """
    from tradex_trading.replay.optimization import make_grid_optimizer
    from tradex_trading.replay.walk_forward import run_walk_forward_by_date

    param_grid = _param_grid(args)
    log.info("Walk-forward %s over %s (train=%dd, test=%dd)",
             args.strategy, param_grid, args.train_window, args.test_window)

    print(f"{'symbol':<12} {'oos-return%':>12} {'oos-sharpe':>10} "
          f"{'steps':>6} {'ok':>4}")
    winners = 0
    for symbol in symbols:
        data = by_symbol[symbol]
        days = len({c.timestamp.date() for c in data})
        if days < args.train_window + args.test_window:
            print(f"{symbol:<12} {'<insufficient data>':>12}")
            continue

        def run_fn(params, data_subset, _symbol=symbol):
            strategy = _make_strategy(args.strategy, _symbol, args, **params)
            return engine.run(strategy, data_subset)

        optimize_fn = make_grid_optimizer(
            param_grid, run_fn, max_workers=args.optimize_workers
        )
        report = run_walk_forward_by_date(
            data, run_fn, optimize_fn,
            train_days=args.train_window,
            test_days=args.test_window,
        )
        print(f"{symbol:<12} {report.aggregate_oos_return * 100:>12.2f} "
              f"{report.aggregate_oos_sharpe:>10.2f} "
              f"{report.total_steps:>6} {report.successful_steps:>4}")
        if report.aggregate_oos_return > 0:
            winners += 1

    log.info("Summary: %d/%d symbols with positive OOS return",
             winners, len(symbols))
    return 0


def _run_portfolio(args, engine: BacktestEngine, candles: list) -> int:
    """Portfolio mode: one ``MultiSymbolSmaCross`` pass over the universe.

    Plain run prints a single aggregate row; ``--optimize`` grid-searches
    fast/slow; ``--optimize --walk-forward`` adds OOS validation over the
    interleaved multi-symbol series.

    Walk-forward uses calendar-date windows (``run_walk_forward_by_date``), so
    every window contains each symbol's bars for the covered dates — unlike
    index slicing, which would fragment the interleaved series.
    """
    from tradex_trading.replay.optimization import grid_search, make_grid_optimizer
    from tradex_trading.replay.walk_forward import run_walk_forward_by_date

    if not args.optimize:
        result = engine.run(_make_portfolio_strategy(args), candles)
        print(f"{'strategy':<12} {'bars':>6} {'trades':>7} {'return%':>9} "
              f"{'sharpe':>7} {'maxDD%':>8} {'fees':>10}")
        print(f"{'PORTFOLIO':<12} {len(candles):>6} {result.num_trades:>7} "
              f"{result.total_return * 100:>9.2f} {result.sharpe:>7.2f} "
              f"{result.max_drawdown * 100:>8.2f} {result.total_fees:>10.2f}")
        return 0

    param_grid = _param_grid(args)
    if args.walk_forward:
        days = len({c.timestamp.date() for c in candles})
        if days < args.train_window + args.test_window:
            log.error("Insufficient data for walk-forward: %d distinct dates", days)
            return 1

        def run_fn(params, data_subset):
            return engine.run(_make_portfolio_strategy(args, **params), data_subset)

        optimize_fn = make_grid_optimizer(
            param_grid, run_fn, max_workers=args.optimize_workers
        )
        report = run_walk_forward_by_date(
            candles, run_fn, optimize_fn,
            train_days=args.train_window,
            test_days=args.test_window,
        )
        print(f"Walk-forward portfolio: steps={report.total_steps} "
              f"ok={report.successful_steps} "
              f"success_rate={report.success_rate:.0%} "
              f"oos_return={report.aggregate_oos_return * 100:.2f}% "
              f"oos_sharpe={report.aggregate_oos_sharpe:.2f}")
        return 0

    search = grid_search(
        param_grid,
        lambda params: engine.run(_make_portfolio_strategy(args, **params), candles),
        max_workers=args.optimize_workers,
    )
    best = search.best
    if best is None:
        log.warning("No valid parameter combos for the portfolio run")
        return 1
    params_str = ",".join(f"{k}={v}" for k, v in best.params.items())
    print(f"best params: {params_str}  return={best.score * 100:.2f}%  "
          f"trades={best.result.num_trades}  sharpe={best.result.sharpe:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
