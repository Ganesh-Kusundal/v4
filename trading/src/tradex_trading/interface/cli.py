"""TradeX v4 CLI — argparse-based command-line interface.

Commands: quote, order, health, scanner, positions, account, orders, watch,
serve, sync.
Uses only stdlib (argparse, json); ``serve`` lazily imports the optional
FastAPI/uvicorn stack.
"""

from __future__ import annotations

import argparse
import json
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
    sync_parser.add_argument("--start", default=None,
                             help="Start date YYYY-MM-DD (default: auto-detect from latest stored data)")
    sync_parser.add_argument("--end", default=None,
                             help="End date YYYY-MM-DD (default: today)")
    sync_parser.add_argument("--universe", default="nifty500",
                             choices=["nifty50", "nifty100", "nifty200", "nifty500"],
                             help="Universe to sync (default: nifty500)")
    sync_parser.add_argument("--timeframe", default="1m",
                             help="Bar resolution (default: 1m)")
    sync_parser.add_argument("--broker", default="dhan",
                             choices=["dhan", "upstox", "both"],
                             help="Broker adapter (default: dhan)")
    sync_parser.add_argument("--workers", type=int, default=4,
                             help="Concurrent fetch threads (default: 4)")
    sync_parser.add_argument("--batch-size", type=int, default=20,
                             help="Symbols per batch (default: 20)")
    sync_parser.add_argument("--skip-existing", action="store_true", default=True,
                             help="Skip symbols with full coverage (default: on)")
    sync_parser.add_argument("--no-skip-existing", action="store_false", dest="skip_existing",
                             help="Re-fetch all symbols regardless of existing data")
    sync_parser.add_argument("--min-gap-stamps", type=int, default=15,
                             help="Ignore gaps shorter than N stamps (default: 15)")
    sync_parser.add_argument("--backoff-base", type=float, default=60.0,
                             help="Initial backoff after a throttled batch, seconds (default: 60)")
    sync_parser.add_argument("--backoff-max", type=float, default=600.0,
                             help="Backoff ceiling, seconds (default: 600)")
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
        from tradex_domain import BrokerId, Equity

        from tradex_trading.config.schema import AppConfig
        from tradex_trading.runtime.startup import boot

        session = boot(AppConfig(broker_id=BrokerId.PAPER, mode="paper"))
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
        from tradex_domain import (
            BrokerId,
            Equity,
            OrderRequest,
            OrderSide,
            OrderType,
            Price,
            Quantity,
        )

        from tradex_trading.config.schema import AppConfig
        from tradex_trading.runtime.startup import boot

        session = boot(AppConfig(broker_id=BrokerId.PAPER, mode="paper"))
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

        from tradex_trading.config.env import load_env_file

        env_path = Path(args.env_file)
        if env_path.exists():
            names = load_env_file(env_path, override=True)
            print(f"env_file={env_path} ({len(names)} entries)")

    from tradex_domain import BrokerId

    from tradex_trading.config.schema import AppConfig
    from tradex_trading.runtime.startup import boot

    session = boot(AppConfig(broker_id=BrokerId.PAPER, mode="paper"))
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
    from tradex_trading.sdk.session import TradingSession

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
    from tradex_trading.sdk.session import TradingSession

    session = TradingSession.paper()
    account = session.broker.get_account()

    print(f"Account: {account.account_id}")
    print(f"Balance: {account.balance}")

    return 0


def cmd_orders(args: Any) -> int:
    """List open orders."""
    from tradex_trading.sdk.session import TradingSession

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


def cmd_serve(args: Any, runtime: Any = None) -> int:
    """Start the TradeX HTTP API (FastAPI + uvicorn) against a booted session.

    Paper mode reuses the bound runtime session when present (``main`` always
    provides one), so ``tradex serve`` never boots a second paper broker.
    Live brokers always boot their own session from the environment; the
    explicit CLI invocation is the confirmation gate.
    """
    from tradex_domain import BrokerId

    from tradex_trading.sdk.session import TradingSession

    broker_id = BrokerId(args.broker)
    reused = broker_id is BrokerId.PAPER and runtime is not None
    session: Any = None
    if reused:
        session = getattr(runtime, "session", None)
        if session is None:
            reused = False
    if session is None:
        if broker_id is BrokerId.PAPER:
            session = TradingSession.paper()
        else:
            session = TradingSession.live(broker_id, confirm=True)
    assert session is not None
    try:
        from tradex_trading.interface.fastapi_app import start_fastapi_server

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
        if not reused:
            session.stop()
    return 0


def cmd_sync(args: Any) -> int:
    """Sync historical OHLCV data for a date range.

    Self-contained: builds its own brokers, fetcher, and store — no
    runtime session needed.  Reuses the proven pattern from
    ``backfill_parquet.py`` and ``fill_gaps.py``.
    """
    import logging
    from datetime import datetime
    from pathlib import Path

    # Parse end date (default: today)
    from datetime import datetime, timedelta
    try:
        end = datetime.strptime(args.end, "%Y-%m-%d") if args.end else datetime.now()
    except ValueError as e:
        print(f"error: invalid end date: {e}")
        return 1

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    # Lazy imports to avoid circular deps
    from tradex_trading.config.env import load_env_file
    from tradex_trading.datalake.gap_detector import GapDetector
    from tradex_trading.datalake.parquet_storage import ParquetStorage
    from tradex_trading.datalake.universe import load_universe
    from tradex_trading.runtime.live import build_broker_from_env

    # Repo root for .env.local and data/
    # cli.py is at trading/src/tradex_trading/interface/cli.py → 5 levels to repo root
    ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
    env_path = ROOT / ".env.local"
    if env_path.exists():
        load_env_file(str(env_path))
    else:
        logging.warning(".env.local not found at %s — broker credentials may be missing", env_path)

    # Build store early so we can auto-detect start date
    store = ParquetStorage(ROOT / "data")

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
        print(f"(Gap detection will skip symbols with full coverage)")

    if end <= start:
        print(f"Nothing to sync — end date ({end.date()}) is before start ({start.date()})")
        return 0

    # Build brokers
    brokers: dict[str, Any] = {}
    if args.dry_run:
        from tradex_brokers.paper.adapter import PaperBroker
        brokers["paper"] = PaperBroker()
    else:
        for name in (["dhan", "upstox"] if args.broker == "both" else [args.broker]):
            try:
                b = build_broker_from_env(name)
                b.connect()
                brokers[name] = b
                logging.info("connected: %s", name)
            except Exception as e:
                logging.warning("skip %s: %s", name, e)

    if not brokers:
        print("error: no brokers available")
        return 1

    # Load universe
    instruments = load_universe(args.universe)
    print(f"Universe: {args.universe} ({len(instruments)} instruments)")
    print(f"Window:   {start.date()} -> {end.date()} ({args.timeframe})")

    # Build sync pipeline — simple_sync is the one fill path (fetch + failover + gaps)
    from tradex_trading.datalake.simple_sync import simple_sync

    gaps = GapDetector(store) if args.skip_existing else None
    broker = brokers.get("dhan") or next(iter(brokers.values()))

    # Run sync
    result = simple_sync(
        broker, store, instruments, args.timeframe, start, end,
        batch_size=args.batch_size,
        max_workers=args.workers,
        gaps=gaps,
    )

    # Report
    print(f"\nSync complete:")
    print(f"  Requested: {result.requested}")
    print(f"  Fetched:   {result.fetched}")
    print(f"  Written:   {result.written} rows")
    if result.failed:
        print(f"  Failed:    {len(result.failed)} symbols")
        print(f"  Examples:  {result.failed[:5]}")

    return 0 if not result.failed else 1


def cmd_watch(args: object) -> None:
    """Watch live quotes for an instrument."""
    from tradex_domain import Equity
    from tradex_domain.value_objects import InstrumentId

    from tradex_trading.sdk.session import TradingSession

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


if __name__ == "__main__":  # python -m tradex_trading.interface.cli serve ...
    raise SystemExit(main())
