"""TradeX v4 CLI — argparse-based command-line interface.

Commands: quote, order, health, scanner, positions, account, orders, watch,
serve, sync.
Uses only stdlib (argparse, json); ``serve`` lazily imports the optional
FastAPI/uvicorn stack.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
from typing import Any


def _build_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser.

    Returns
    -------
    argparse.ArgumentParser
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="tradex-v4", description="TradeX v4 Trading Platform"
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="optional KEY=VALUE file to load with override (explicit opt-in)",
    )
    sub = parser.add_subparsers(dest="command")

    # quote command
    q = sub.add_parser("quote", help="Get quote for an instrument")
    q.add_argument("exchange", help="Exchange (NSE, BSE, NFO, etc.)")
    q.add_argument("symbol", help="Symbol (e.g., RELIANCE)")

    # order command
    o = sub.add_parser("order", help="Place an order")
    o.add_argument("exchange")
    o.add_argument("symbol")
    o.add_argument("side", choices=["BUY", "SELL"])
    o.add_argument("quantity", type=int)
    o.add_argument("--type", default="MARKET", choices=["MARKET", "LIMIT"])
    o.add_argument("--price", type=float, default=None)

    # health command
    sub.add_parser("health", help="Check system health")

    # scanner command
    sub.add_parser("scanner", help="Run scanner")

    # positions command
    pos_parser = sub.add_parser("positions", help="List all positions")
    pos_parser.set_defaults(func=cmd_positions)

    # account command
    acc_parser = sub.add_parser("account", help="Show account info")
    acc_parser.set_defaults(func=cmd_account)

    # orders command
    ord_parser = sub.add_parser("orders", help="List orders")
    ord_parser.set_defaults(func=cmd_orders)

    # watch command
    watch_parser = sub.add_parser("watch", help="Watch live quotes for an instrument")
    watch_parser.add_argument("instrument", help="Instrument ID (e.g., NSE:RELIANCE)")
    watch_parser.add_argument("--count", type=int, default=10, help="Number of quotes to show")
    watch_parser.set_defaults(func=cmd_watch)

    # serve command — TradeX HTTP API (FastAPI + uvicorn)
    serve_parser = sub.add_parser("serve", help="Start the TradeX HTTP API (FastAPI + uvicorn)")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    serve_parser.add_argument("--port", type=int, default=8080, help="Bind port (default 8080)")
    serve_parser.add_argument(
        "--broker",
        default="PAPER",
        type=str.upper,
        choices=["PAPER", "DHAN", "UPSTOX"],
        help="Broker to serve, case-insensitive (default PAPER; live brokers require credentials)",
    )
    serve_parser.add_argument(
        "--api-key", default=None, help="Optional API key required on requests"
    )
    serve_parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="uvicorn worker processes (default 1; >1 builds a fresh session per worker)",
    )
    serve_parser.add_argument(
        "--reload",
        action="store_true",
        help="uvicorn auto-reload on source changes (dev; rebuilds the session on each restart)",
    )
    serve_parser.set_defaults(func=cmd_serve)

    # sync command — historical OHLCV data sync
    sync_parser = sub.add_parser("sync", help="Sync historical OHLCV data")
    sync_parser.add_argument(
        "--start", default=None,
        help="Start date YYYY-MM-DD (default: auto-detect from latest stored data)")
    sync_parser.add_argument("--end", default=None,
                             help="End date YYYY-MM-DD (default: today)")
    sync_parser.add_argument(
        "--universe", default="nifty500",
        choices=["nifty50", "nifty100", "nifty200", "nifty500"],
        help="Universe to sync (default: nifty500)")
    sync_parser.add_argument("--timeframe", default="1m", choices=["1m"],
                             help="Bar resolution (Dhan 1-minute only)")
    sync_parser.add_argument("--workers", type=int, default=2,
                             help="Concurrent fetch threads (default: 2)")
    sync_parser.add_argument("--batch-size", type=int, default=20,
                             help="Symbols per batch (default: 20)")
    sync_parser.add_argument("--skip-existing", action="store_true", default=True,
                             help="Skip symbols with full coverage (default: on)")
    sync_parser.add_argument("--no-skip-existing", action="store_false", dest="skip_existing",
                             help="Re-fetch all symbols regardless of existing data")
    sync_parser.add_argument("--min-gap-stamps", type=int, default=1,
                             help="Leave holes shorter than N stamps unfetched (default: 1)")
    sync_parser.add_argument("--tail-days", type=int, default=None,
                             help="Scan only each symbol's last N stored days "
                                  "(default: the whole window)")
    sync_parser.add_argument("--no-open-stamps", action="store_true",
                             help="Do not fetch a missing 09:15 bar")
    sync_parser.add_argument("--only-symbols", default="",
                             help="Comma-separated symbols to sync, nothing else")
    sync_parser.add_argument("--skip-symbols", default="",
                             help="Comma-separated symbols to leave out. A symbol "
                                  "that returns empty is usually resolving to the "
                                  "wrong security; check that first")
    sync_parser.add_argument("--budget", type=float, default=480.0,
                             help="Seconds to work before exiting resumable (default: 480)")
    sync_parser.add_argument("--backoff-base", type=float, default=1.0,
                             help="Initial backoff after a throttled batch, seconds (default: 1)")
    sync_parser.add_argument("--dry-run", action="store_true",
                             help="Use PaperBroker with synthetic data")
    sync_parser.set_defaults(func=cmd_sync)

    return parser


def run_cli(argv: list[str] | None = None, runtime: Any | None = None) -> int:
    """Run the CLI with optional runtime context.

    Parameters
    ----------
    argv : list[str] | None
        Command-line arguments. If None, uses sys.argv.
    runtime : Any | None
        Optional runtime context (session, config, etc.).

    Returns
    -------
    int
        Exit code.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse errors exit(2)
        code = exc.code if isinstance(exc.code, int) else 2
        return code

    if args.command is None:
        parser.print_help(sys.stderr)
        return 2

    if args.command == "health":
        if runtime is None:
            print("ok")
            return 0
        state = runtime.session.state.value
        print(f"session={state} environment={runtime.config.environment}")
        return 0

    if args.command == "serve":
        # Reuses a bound runtime session in paper mode (avoids a second
        # boot); live brokers always boot their own from the environment.
        return args.func(args, runtime)

    if args.command == "sync":
        # Sync is self-contained — no runtime session needed.
        return args.func(args)

    if runtime is None:
        print("no runtime bound")
        return 1

    if args.command == "quote":
        from tradex_config.env import from_env
        from tradex_domain import Equity
        from tradex_runtime.startup import boot

        # via from_env, not a hand-built AppConfig: AC-10 makes a missing risk
        # limit a refusal to boot, and from_env is the only route that can
        # carry a complete policy (all five limits, from TRADEX_RISK_*).
        session = boot(from_env())
        try:
            eq = Equity.of(args.exchange, args.symbol)
            quote = session.broker.ltp(eq)
            print(json.dumps({"symbol": args.symbol, "ltp": str(quote.value)}, indent=2))
        except KeyError:
            print(f"no quote for {args.symbol}")
            return 0
        except Exception as exc:  # noqa: BLE001 — loud CLI failure
            print(f"quote failed: {exc}")
            return 1
        finally:
            session.stop()
        return 0

    if args.command == "order":
        from tradex_config.env import from_env
        from tradex_domain import (
            Equity,
            OrderRequest,
            OrderSide,
            OrderType,
            Price,
            Quantity,
        )
        from tradex_runtime.startup import boot

        # via from_env: same reason as the quote command above.
        session = boot(from_env())
        try:
            eq = Equity.of(args.exchange, args.symbol)
            req = OrderRequest(
                instrument=eq,
                side=OrderSide(args.side),
                order_type=OrderType(args.type),
                quantity=Quantity(Decimal(str(args.quantity))),
                price=Price(Decimal(str(args.price))) if args.price else None,
            )
            receipt = session.engine.submit(req)
            print(
                json.dumps(
                    {"order_id": receipt.order_id.value, "status": receipt.status},
                    indent=2,
                )
            )
        except Exception as exc:  # noqa: BLE001 — loud CLI failure
            print(f"order failed: {exc}")
            return 1
        finally:
            session.stop()
        return 0

    if args.command == "scanner":
        print(json.dumps({"results": []}, indent=2))
        return 0

    if args.command in ("positions", "account", "orders", "watch"):
        if hasattr(args, "func"):
            return args.func(args)

    parser.print_help(sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    """TradeX v4 CLI entry point (console script ``tradex``).

    Parses arguments first (the old implementation read ``args.env_file``
    before ``args`` existed and crashed with ``UnboundLocalError`` on every
    invocation), loads an optional env file, boots the paper session, then
    delegates command dispatch to :func:`run_cli` — the single source of
    truth for command behaviour. Passing a runtime keeps the quote/order
    commands functional (without one, ``run_cli`` prints "no runtime bound").

    Returns
    -------
    int
        Exit code (0 on success).
    """
    import types

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse errors exit(2); --help exits 0
        return exc.code if isinstance(exc.code, int) else 2

    # Load optional env file before boot (explicit opt-in, v3 pattern).
    if args.env_file:
        from pathlib import Path

        from tradex_config.env import load_env_file

        env_path = Path(args.env_file)
        if env_path.exists():
            names = load_env_file(env_path, override=True)
            # AC-17 / lane 5d: record which credential file was loaded so boot
            # can assert it is not readable beyond its owner. Set BEFORE boot
            # runs, and only when the file actually exists.
            os.environ["TRADEX_ENV_FILE"] = str(env_path)
            print(f"env_file={env_path} ({len(names)} entries)")

    if args.command == "sync":
        # Sync is self-contained (see run_cli) — it builds its own brokers,
        # fetcher, and store. Booting a session here only to discard it made
        # `tradex sync` unusable: the AC-10 risk gate refuses to boot without a
        # complete policy, so the command died before reaching its own dispatch.
        return run_cli(argv)

    from tradex_config.env import from_env
    from tradex_runtime.startup import boot

    # via from_env: the operator's TRADEX_RISK_* policy has to reach boot, and
    # a literal AppConfig here cannot carry one (AC-10 refuses to boot without
    # a complete risk policy, which made every `tradex` command unusable).
    #
    # The reconciliation monitor is built here, not in cmd_serve, because this
    # is the session ``tradex serve`` reuses in paper mode: cmd_serve skips its
    # own boot for a bound runtime, so a monitor built only there would never
    # exist for a paper server. quote/order are one-shot commands and pass no
    # factory — a daemon thread that outlives a single CLI call is not useful.
    session = boot(
        from_env(),
        reconciliation_monitor_factory=build_reconciliation_monitor,
    )
    runtime = types.SimpleNamespace(
        session=session,
        config=types.SimpleNamespace(environment=session.mode.upper()),
    )
    try:
        return run_cli(argv, runtime=runtime)
    finally:
        session.stop()


def cmd_positions(args: Any) -> int:
    """List all positions with P&L."""
    from tradex_runtime.session import TradingSession

    session = TradingSession.paper()
    positions = session.engine.cache.all_positions()

    if not positions:
        print("No positions")
        return 0

    print(f"{'Symbol':<20} {'Qty':>10} {'Avg Price':>12} {'LTP':>12} {'P&L':>12}")
    print("-" * 70)

    for pos in positions:
        iid = pos.instrument.instrument_id
        symbol = f"{iid.exchange}:{iid.underlying}"
        qty = str(pos.quantity.value)
        avg_price = str(pos.avg_price.value)
        # LTP would come from market service in real usage
        ltp = "N/A"
        pnl = str(pos.market_value)
        print(f"{symbol:<20} {qty:>10} {avg_price:>12} {ltp:>12} {pnl:>12}")

    return 0


def cmd_account(args: Any) -> int:
    """Show account balance and margin."""
    from tradex_runtime.session import TradingSession

    session = TradingSession.paper()
    account = session.broker.get_account()

    print(f"Account: {account.account_id}")
    print(f"Balance: {account.balance}")

    return 0


def cmd_orders(args: Any) -> int:
    """List open orders."""
    from tradex_runtime.session import TradingSession

    session = TradingSession.paper()
    orders = session.engine.all_orders()

    if not orders:
        print("No orders")
        return 0

    print(f"{'Order ID':<20} {'Symbol':<20} {'Side':<8} {'Status':<12} {'Qty':>10}")
    print("-" * 75)

    for order in orders:
        order_id = str(order.order_id)
        oid = order.instrument.instrument_id
        symbol = f"{oid.exchange}:{oid.underlying}"
        side = order.side.value
        status = order.status.value
        qty = str(order.quantity.value)
        print(f"{order_id:<20} {symbol:<20} {side:<8} {status:<12} {qty:>10}")

    return 0


def build_reconciliation_monitor(broker, cache, engine, metrics):
    """Build the reconciliation daemon for a live session.

    The single composition helper for lane 5e. The two halves of the daemon sit
    on opposite sides of the package boundary: ``build_reconciliation_monitor_seams``
    is in ``tradex_interfaces`` (it binds the adoption policy to the OMS book)
    and ``ReconciliationMonitor`` is in ``tradex_runtime``, which may not import
    ``tradex_interfaces`` (see ``tests/test_import_boundaries.py``). This module
    is the composition root that can see both.

    Callers pass this as ``reconciliation_monitor_factory`` to ``boot()``, which
    invokes it *before* ``session.start()`` — a monitor assigned post-hoc would
    be constructed and never started, because ``boot()`` starts the session
    before it returns.
    """
    from tradex_runtime.reconciliation_monitor import (
        ReconciliationMonitor,
    )

    from tradex_interfaces.safety import (
        build_reconciliation_monitor_seams,
    )

    seams = build_reconciliation_monitor_seams(
        broker=broker, cache=cache, engine=engine,
    )
    return ReconciliationMonitor(
        reconcile=seams.reconcile,
        adopt=seams.adopt,
        metrics=metrics,
    )


def cmd_serve(args: Any, runtime: Any = None) -> int:
    """Start the TradeX HTTP API (FastAPI + uvicorn) against a booted session.

    Paper mode reuses the bound runtime session when present (``main`` always
    provides one), so ``tradex serve`` never boots a second paper broker.
    Live brokers always boot their own session from the environment; the
    explicit CLI invocation is the confirmation gate.
    """
    from tradex_domain import BrokerId
    from tradex_runtime.startup import boot

    from tradex_interfaces.fastapi_app import (
        _require_api_key_for_live,
        _require_live_bind_loopback,
        start_fastapi_server,
    )

    broker_id = BrokerId(args.broker)
    mode = "paper" if broker_id is BrokerId.PAPER else "live"
    reused = broker_id is BrokerId.PAPER and runtime is not None
    session: Any = None
    try:
        _require_api_key_for_live(mode, args.api_key)
        _require_live_bind_loopback(broker_id, mode, args.host)
        if reused:
            session = getattr(runtime, "session", None)
            if session is None:
                reused = False
        if session is None:
            # Both constructions go through from_env: AC-10 refuses to boot a
            # session with an incomplete risk policy, and neither the
            # TradingSession.paper()/live() factories nor a literal AppConfig
            # can carry one. from_env is the only route that reads the
            # operator's TRADEX_RISK_* policy. The explicit --broker argument
            # is honoured over the env default.
            from tradex_config.env import from_env, serve_config

            config = serve_config(from_env(), broker_id=broker_id, mode=mode)
            # Build the reconciliation daemon from inside the boot, not after
            # it: boot() calls session.start() before it returns, so a monitor
            # assigned post-hoc would never be started. Passing a factory lets
            # boot() construct it and hand it to the session, which then owns
            # the lifecycle (start in start(), stop in stop() before the broker
            # is torn down). See build_reconciliation_monitor for why the
            # factory is defined here rather than in tradex_runtime.
            session = boot(
                config,
                wire_strategies=(
                    bool(config.execution.strategy_ids) if mode == "live" else False
                ),
                reconciliation_monitor_factory=build_reconciliation_monitor,
            )
        assert session is not None
        start_fastapi_server(
            session,
            host=args.host,
            port=args.port,
            api_key=args.api_key,
            workers=args.workers,
            reload=args.reload,
        )
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 — loud CLI failure
        print(f"serve failed: {exc}")
        return 1
    finally:
        if session is not None and not reused:
            session.stop()
    return 0


def cmd_sync(args: Any) -> int:
    """Sync historical OHLCV data for a date range.

    Self-contained: builds its own broker and store — no runtime session
    needed. This is the one sync path: scan, fetch only the holes, re-scan.
    One run per lake at a time.
    """
    from pathlib import Path

    from tradex_market_data.paths import datalake_root
    from tradex_market_data.sync_service import SyncLocked, sync_lock

    try:
        with sync_lock(Path(datalake_root())):
            return _run_sync(args)
    except SyncLocked as e:
        print(f"error: {e}")
        return 1


def _run_sync(args: Any) -> int:
    import logging
    from datetime import datetime, timedelta
    from pathlib import Path

    from tradex_domain.market_calendar import (
        IST,
        NSE_HOLIDAY_YEARS,
        to_ist_naive,
    )

    # Naive IST wall clock, whatever zone the host runs in.
    try:
        end = (datetime.strptime(args.end, "%Y-%m-%d") if args.end
               else to_ist_naive(datetime.now(IST)))
    except ValueError as e:
        print(f"error: invalid end date: {e}")
        return 1

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    # Lazy imports to avoid circular deps
    from tradex_config.env import load_env_file
    from tradex_domain.universe import load_universe
    from tradex_market_data.parquet_storage import ParquetStorage
    from tradex_market_data.paths import datalake_root
    from tradex_runtime.live import build_broker_from_env

    # Repo root for .env.local and data/
    # Extracted: interfaces/src/tradex_interfaces/cli.py → parents[3] == repo
    ROOT = Path(__file__).resolve().parents[3]
    env_path = ROOT / ".env.local"
    if env_path.exists():
        load_env_file(str(env_path))
    else:
        logging.warning(".env.local not found at %s — broker credentials may be missing", env_path)

    # Build store early so we can auto-detect start date.
    # Resolved through the shared seam, not a second ROOT/"data" literal: the
    # readers (chart/stream routes) honour $TRADEX_DATALAKE_ROOT, so a sync that
    # hardcoded ROOT/"data" would write bars into a lake the server never reads.
    store = ParquetStorage(Path(datalake_root()))

    # Auto-detect start date from store if not provided
    if args.start:
        try:
            start = datetime.strptime(args.start, "%Y-%m-%d")
        except ValueError as e:
            print(f"error: invalid start date: {e}")
            return 1
    else:
        # Default to 30 days ago — gap detection will skip symbols with full coverage
        start = end - timedelta(days=30)
        print(f"No --start provided. Syncing from: {start.date()} -> {end.date()}")
        print("(Gap detection will skip symbols with full coverage)")

    if end <= start:
        print(f"Nothing to sync — end date ({end.date()}) is before start ({start.date()})")
        return 0

    uncovered = {start.year, end.year} - NSE_HOLIDAY_YEARS
    if uncovered:
        print(f"error: no NSE holiday calendar for {sorted(uncovered)}; every exchange "
              "holiday would read as a missing session. Add the year to "
              "tradex_domain.market_calendar first.")
        return 1

    from tradex_brokers import DhanBroker, PaperBroker

    broker: PaperBroker | DhanBroker
    if args.dry_run:
        broker = PaperBroker()
        logging.info("connected: paper (dry-run)")
    else:
        try:
            broker = build_broker_from_env("dhan")
            broker.connect()
            logging.info("connected: dhan")
        except Exception as e:
            print(f"error: dhan unavailable: {e}")
            return 1

    # Load universe
    instruments = load_universe(args.universe)
    only = {s.strip().upper() for s in args.only_symbols.split(",") if s.strip()}
    skip = {s.strip().upper() for s in args.skip_symbols.split(",") if s.strip()}
    unknown = only - {i.symbol for i in instruments}
    if unknown:
        print(f"error: not in {args.universe}: {sorted(unknown)}")
        return 1
    instruments = [
        i for i in instruments
        if i.symbol not in skip and (not only or i.symbol in only)
    ]
    if not instruments:
        print("error: no instruments selected")
        return 1
    if not args.dry_run:
        from tradex_market_data.symbol_resolve import resolve_universe_symbols
        resolved = resolve_universe_symbols(broker, instruments)
        if resolved.renamed:
            print(f"Renamed:  {resolved.renamed[:10]}")
        if resolved.quarantine:
            print(f"Quarantine ({len(resolved.quarantine)}): {resolved.quarantine[:10]}")
        instruments = resolved.ok
    print(f"Universe: {args.universe} ({len(instruments)} instruments)")
    print(f"Window:   {start.date()} -> {end.date()} ({args.timeframe})")

    from tradex_domain.market_calendar import NSE_HOLIDAYS_2026
    from tradex_market_data.sync_service import SyncConfig, SyncService

    svc = SyncService(
        broker, store,
        SyncConfig(
            timeframe=args.timeframe,
            batch_size=args.batch_size,
            max_workers=args.workers,
            min_gap_stamps=args.min_gap_stamps,
            tail_days=args.tail_days,
            include_open_stamps=not args.no_open_stamps,
            budget_seconds=args.budget,
            initial_backoff_s=args.backoff_base,
        ),
    )

    if args.skip_existing:
        # converge scans, fetches only the holes, and re-scans until clean or
        # the budget is spent — one path, bounded, instead of a bare fetch.
        result = svc.converge(instruments, start, end, holidays=NSE_HOLIDAYS_2026)
        if result.under_floored:
            print(f"  Under floor ({args.min_gap_stamps} stamps): "
                  f"{len(result.under_floored)} symbols — lower --min-gap-stamps "
                  f"to fetch them")
    else:
        result = svc.sync(instruments, start, end, holidays=NSE_HOLIDAYS_2026)

    print("\nSync complete:")
    print(f"  Requested: {result.requested}")
    print(f"  Fetched:   {result.fetched}")
    print(f"  Written:   {result.written} rows")
    print(f"  Elapsed:   {result.elapsed_s:.0f}s")
    if args.skip_existing:
        print(f"  Converged: {result.converged}")
    if result.skipped:
        print(f"  Skipped:   {len(result.skipped)} (no data)")
    if result.failed:
        print(f"  Failed:    {len(result.failed)} symbols")
        print(f"  Examples:  {list(result.failed)[:5]}")

    if args.dry_run:
        return 0
    if args.skip_existing:
        return 0 if result.converged and not result.failed else 1
    return 0 if not result.failed and not result.resumable else 1


def cmd_watch(args: object) -> None:
    """Watch live quotes for an instrument."""
    from tradex_domain import Equity
    from tradex_domain.value_objects import InstrumentId
    from tradex_runtime.session import TradingSession

    instrument_id = getattr(args, "instrument", "")
    count = getattr(args, "count", 10)

    session = TradingSession.paper()
    session.start()

    iid = InstrumentId.parse(instrument_id)
    instrument = Equity.of(iid.exchange, iid.underlying)
    print(f"Watching {instrument_id} (showing {count} quotes)...")
    print("-" * 50)

    try:
        for i in range(count):
            try:
                quote = session.broker.get_quote(instrument)
                if quote:
                    print(f"  LTP: {quote.ltp}")
                else:
                    print(f"  No quote available (attempt {i+1}/{count})")
            except Exception as e:
                print(f"  Error: {e}")
            if i < count - 1:
                import time
                time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        session.stop()


__all__ = ["main", "run_cli"]


if __name__ == "__main__":  # python -m tradex_interfaces.cli serve ...
    raise SystemExit(main())
