#!/usr/bin/env python3
"""Seed the smallest datalake that makes the chart render real bars.

Usage::

    python trading/scripts/seed_e2e_datalake.py                 # default fixture
    python trading/scripts/seed_e2e_datalake.py --days 8        # longer history
    python trading/scripts/seed_e2e_datalake.py --root /tmp/lake --force

Why this exists
---------------
``data/ohlcv/`` is gitignored (281MB of binary churn, rebuilt by
``backfill_parquet.py``), so a fresh clone and CI have **no market data at all**.
The chart then answers ``source: "none"`` with zero bars, and every browser-level
test is vacuous — it passes against ``no bars for this range`` without touching
the code under test. This writes just enough history for the real read path
(``ParquetStorage`` -> ``candles_from_dataframe`` -> ``HistoricalSeries``) to
produce real bars.

Why it is safe to run anywhere
-----------------------------
**Idempotent and non-destructive.** If the symbol already has session bars, this
exits without writing. A developer with the full lake keeps it byte-identical;
CI and a fresh clone get a fixture. Nothing here touches another symbol.

``ParquetStorage.upsert`` insert-or-replaces by ``(symbol, timeframe,
timestamp)``; it never truncates. So ``--force`` overwrites the bars it
regenerates and leaves any others standing, and ``--days`` can only ever *widen*
coverage. For a clean slate pass ``--root`` at an empty directory.

Why it uses the production writer
---------------------------------
Bars go through ``ParquetStorage.upsert``, so the fixture carries the real
schema, the real OHLC validation and the real session mask. A hand-rolled
``to_parquet`` could write rows the store would reject on the way back in, and
the fixture would then test a datalake that cannot exist.

Determinism
-----------
Prices come from a fixed seed, so a given ``--seed``/``--days`` always produces
the same bars. The default end date is the most recent weekday, which affects
only *when* the bars are, never their values.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

# Ensure project packages are importable
ROOT = Path(__file__).resolve().parent.parent.parent  # repo root
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))

from tradex_domain import Timeframe  # noqa: E402 — sys.path setup above
from tradex_domain.market_calendar import (  # noqa: E402 — sys.path setup above
    NSE_HOLIDAYS_2026,
)

from tradex_trading.datalake.parquet_storage import (  # noqa: E402 — sys.path setup above
    ParquetStorage,
)
from tradex_trading.datalake.paths import DATALAKE_ROOT  # noqa: E402 — sys.path setup above

#: One bar per minute, 09:15 .. 15:29 IST — 375 bars, matching the real session
#: (``market_session_mask`` also admits 15:30, but no NSE equity bar is stamped
#: there; generating one would make the fixture differ from production data).
SESSION_START = time(9, 15)
SESSION_MINUTES = 375

DEFAULT_SYMBOL = "RELIANCE"
DEFAULT_DAYS = 5
DEFAULT_SEED = 20260913
START_PRICE = 1275.0


def _session_days(end: date, count: int) -> list[date]:
    """The ``count`` NSE session days on or before ``end``, oldest first."""
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5 and cursor not in NSE_HOLIDAYS_2026:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def _bars(symbol: str, days: list[date], seed: int) -> pd.DataFrame:
    """A random-walk session series with OHLC-valid, positive bars.

    The walk is continuous across days (no overnight jump) so that any
    resampled or daily view still has a coherent close-to-open series.
    """
    rng = random.Random(seed)
    rows: list[dict[str, object]] = []
    price = START_PRICE

    for day in days:
        open_dt = datetime.combine(day, SESSION_START)
        for minute in range(SESSION_MINUTES):
            ts = open_dt + timedelta(minutes=minute)
            open_px = round(price, 2)
            # Sub-rupee drift keeps the series plausible for an equity at
            # ~1,275 without the rounding ever crossing a tick.
            close_px = round(open_px + rng.uniform(-0.9, 0.9), 2)
            wick_up = round(rng.uniform(0.05, 1.4), 2)
            wick_down = round(rng.uniform(0.05, 1.4), 2)
            rows.append(
                {
                    "symbol": symbol,
                    "exchange": "NSE",
                    "kind": "equity",
                    "timeframe": Timeframe.M1.value,
                    "timestamp": ts,
                    "open": open_px,
                    "high": round(max(open_px, close_px) + wick_up, 2),
                    "low": round(min(open_px, close_px) - wick_down, 2),
                    "close": close_px,
                    "volume": rng.randint(1_000, 40_000),
                }
            )
            price = close_px

    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--root",
        default=DATALAKE_ROOT,
        help="datalake root (default: the repo-anchored one the server reads)",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="last session day, YYYY-MM-DD (default: most recent weekday)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="regenerate even when the symbol already has bars (overwrites the overlap)",
    )
    args = parser.parse_args(argv)

    if args.days < 1:
        parser.error("--days must be >= 1")

    store = ParquetStorage(args.root)

    if not args.force:
        existing = store.read(symbols=[args.symbol], timeframe=Timeframe.M1.value)
        if not existing.empty:
            print(
                f"seed_e2e_datalake: {args.symbol} already has {len(existing)} bars "
                f"in {args.root} — nothing to do"
            )
            return 0

    end = date.fromisoformat(args.end) if args.end else date.today()
    days = _session_days(end, args.days)
    frame = _bars(args.symbol, days, args.seed)
    written = store.upsert(frame)

    if written != len(frame):
        # ParquetStorage drops bars failing the OHLC/session contract, so a
        # shortfall means the generator, not the store, is wrong.
        print(
            f"seed_e2e_datalake: FAILED — the store accepted {written} of "
            f"{len(frame)} generated bars",
            file=sys.stderr,
        )
        return 1

    # The stored total, not `written`: upsert merges, so a lake that already
    # held bars for this symbol reports both numbers rather than one misleading
    # one.
    stored = store.read(symbols=[args.symbol], timeframe=Timeframe.M1.value)
    print(
        f"seed_e2e_datalake: {args.symbol} wrote {written} bars "
        f"({days[0]}..{days[-1]}); {len(stored)} stored -> {args.root}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
