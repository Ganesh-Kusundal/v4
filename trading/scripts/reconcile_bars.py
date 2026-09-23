#!/usr/bin/env python3
"""Reconcile stored bars against a broker: rewrite days that disagree.

Every other datalake tool asks whether bars are *present*. This one asks whether
the ones present are *right*, and fixes the ones that are not — the class of
defect a gap detector cannot see, because the bars exist and are simply the
wrong bars:

* **A bad print.** ``IRB`` holds all of 2026-03-30 at ~11.35 while the broker
  and both neighbouring days say ~22. It is a -46% one-day move, past NSE's
  widest circuit band, that fully reverts — the lake inherited one broker's
  value and nothing could catch it, because the bars are there.
* **A re-based series.** ``ANANDRATHI``'s whole pre-2026-06-03 history sits at
  exactly 2x what the broker reports: a 2:1 split re-based the vendor's history
  and the lake predates it. A backtest crossing that date sees a phantom -49%
  crash. Re-fetching replaces price *and* volume, which scaling by the ratio
  would not.

The comparison is day-level (a day's high close, which is what a level change or
a bad print moves) and the verdict separates three cases the numbers alone
would blur: a day both sides agree on, a day they do not, and a day only one
side has at all. A ``source_only`` day is normally not a gap: brokers serve
phantom sessions the storage guard drops at the write chokepoint (2026-02-01
is a Sunday in both masters), so it is reported and deliberately not written.

``upsert`` replaces by ``(symbol, timeframe, timestamp)``, so re-fetching a day
overwrites exactly the bars it covers and adds any the lake lacked. Two guards
keep that from doing harm: a day is only rewritten when the broker returns
essentially as many bars as the lake already holds (a thinner response would
leave half a day stale at the old level), and the whole run defaults to a dry
run until ``--apply``.

Usage::

    python trading/scripts/reconcile_bars.py --symbols IRB
    python trading/scripts/reconcile_bars.py --symbols ANANDRATHI --apply
    python trading/scripts/reconcile_bars.py --symbols IRB,ANANDRATHI --json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))

from tradex_domain.instruments import Equity  # noqa: E402

from tradex_trading.datalake.parquet_storage import ParquetStorage  # noqa: E402
from tradex_trading.datalake.paths import datalake_root  # noqa: E402
from tradex_trading.datalake.simple_sync import series_to_frame  # noqa: E402

log = logging.getLogger("reconcile-bars")

#: Days a broker accepts in one history call (Upstox caps at 30) — well under.
_DEFAULT_WINDOW_DAYS = 20

#: A day's high close may differ this much before it counts as a disagreement.
_DEFAULT_TOL = 0.005

#: A rewrite needs this share of the bars the lake already holds, else the day
#: would be left half-overwritten at the old level.
_DEFAULT_MIN_COVERAGE = 0.95

#: ...and at least this many bars outright, so a near-empty response cannot
#: overwrite a full day just because the lake was also thin.
_DEFAULT_MIN_BARS = 300


@dataclass
class DayComparison:
    """One stored day versus the broker's version of it."""

    day: str
    status: str
    lake_close: float | None = None
    source_close: float | None = None
    lake_bars: int | None = None
    source_bars: int | None = None
    volume_ratio: float | None = None
    rewrite: bool = False
    reason: str = ""

    @property
    def ratio(self) -> float | None:
        if self.lake_close is None or self.source_close is None or self.source_close == 0:
            return None
        return self.lake_close / self.source_close


@dataclass
class SymbolReport:
    """Everything the run learned about one symbol."""

    symbol: str
    days: list[DayComparison] = field(default_factory=list)
    days_rewritten: int = 0
    rows_written: int = 0
    skipped: list[str] = field(default_factory=list)
    residual: int = 0

    @property
    def compared(self) -> int:
        return len(self.days)

    @property
    def disagreed(self) -> list[DayComparison]:
        return [d for d in self.days if d.status == "disagree"]

    def ratios(self) -> list[float]:
        return [r for r in (d.ratio for d in self.disagreed) if r is not None]


def daily_aggregate(frame: pd.DataFrame) -> pd.DataFrame:
    """Per-day comparison metrics from a bar frame.

    A day is characterised by its **high close** — which is what a level change
    or a bad print moves — plus its bar count (the coverage guard) and volume
    (informational: a split re-bases volume too, a bad print does not).
    """
    columns = ["symbol", "day", "bars", "close", "volume"]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    working = frame.copy()
    working["day"] = pd.to_datetime(working["timestamp"]).dt.date
    return (
        working.groupby(["symbol", "day"], as_index=False)
        .agg(bars=("close", "size"), close=("close", "max"), volume=("volume", "sum"))
    )


def compare_days(lake: pd.DataFrame, source: pd.DataFrame, *, tol: float,
                 min_coverage: float, min_bars: int) -> list[DayComparison]:
    """Classify each day as agree / disagree / present on only one side.

    The rewrite decision is deliberately stricter than the disagreement: saying
    two versions differ is cheap, but replacing a stored day is only safe when
    the broker actually covers it.
    """
    merged = lake.merge(source, on=["symbol", "day"], how="outer",
                        suffixes=("_lake", "_src"))
    out: list[DayComparison] = []
    for row in merged.itertuples():
        lake_close = None if pd.isna(row.close_lake) else float(row.close_lake)
        source_close = None if pd.isna(row.close_src) else float(row.close_src)
        lake_bars = None if pd.isna(row.bars_lake) else int(row.bars_lake)
        source_bars = None if pd.isna(row.bars_src) else int(row.bars_src)
        volume_ratio = None
        if not pd.isna(row.volume_lake) and not pd.isna(row.volume_src) and row.volume_src:
            volume_ratio = float(row.volume_lake) / float(row.volume_src)
        day = DayComparison(
            day=str(row.day), status="agree",
            lake_close=lake_close, source_close=source_close,
            lake_bars=lake_bars, source_bars=source_bars, volume_ratio=volume_ratio,
        )
        if lake_close is None:
            day.status = "source_only"
        elif source_close is None:
            day.status = "lake_only"
        elif abs(day.ratio - 1.0) > tol:  # type: ignore[operator]
            day.status = "disagree"
        if day.status == "disagree":
            needed = max(min_bars, int((lake_bars or 0) * min_coverage))
            if (source_bars or 0) < needed:
                day.reason = (f"broker returned {source_bars} bars, lake holds "
                              f"{lake_bars}; need {needed}")
            else:
                day.rewrite = True
        out.append(day)
    return out


def fetch_source(broker: Any, symbol: str, start: datetime, end: datetime,
                 *, window_days: int, timeframe: str = "1m") -> pd.DataFrame:
    """Broker bars for ``symbol`` over the range, in cap-sized windows."""
    instrument = Equity.of("NSE", symbol)
    frames: list[pd.DataFrame] = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=window_days - 1), end)
        series = broker.history(
            instrument, timeframe,
            cursor.replace(hour=9, minute=15, second=0, microsecond=0),
            stop.replace(hour=15, minute=35, second=0, microsecond=0),
        )
        frame = series_to_frame(series, symbol)
        if not frame.empty:
            frames.append(frame)
        cursor = stop + timedelta(days=1)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _lake_span(store: ParquetStorage, symbol: str) -> tuple[datetime, datetime] | None:
    """First and last stored timestamp for *symbol*."""
    frame = store.read(symbols=[symbol])
    if frame.empty:
        return None
    stamps = pd.to_datetime(frame["timestamp"])
    return stamps.min().to_pydatetime(), stamps.max().to_pydatetime()


def reconcile_symbol(store: ParquetStorage, broker: Any, symbol: str, *,
                     start: datetime, end: datetime, tol: float,
                     min_coverage: float, min_bars: int, window_days: int,
                     apply: bool) -> SymbolReport:
    """Compare one symbol's stored bars with the broker, rewriting if asked."""
    report = SymbolReport(symbol=symbol)
    lake_frame = store.read(symbols=[symbol], start=start, end=end)
    source_frame = fetch_source(broker, symbol, start, end, window_days=window_days)
    if source_frame.empty:
        report.skipped.append(f"{symbol}: broker returned no bars for "
                              f"{start:%Y-%m-%d}..{end:%Y-%m-%d}")
        return report

    report.days = compare_days(
        daily_aggregate(lake_frame), daily_aggregate(source_frame),
        tol=tol, min_coverage=min_coverage, min_bars=min_bars,
    )
    report.skipped = [f"{d.day}: {d.reason}" for d in report.days if d.reason]

    if not apply:
        return report

    targets = {d.day for d in report.days if d.rewrite}
    if not targets:
        return report
    days = pd.to_datetime(source_frame["timestamp"]).dt.date
    payload = source_frame[days.astype(str).isin(targets)]
    report.rows_written = store.upsert(payload)
    report.days_rewritten = len(targets)

    # Verify by re-reading: a repair announced but not confirmed is the exact
    # failure mode that let these two days sit wrong for months.
    after = store.read(symbols=[symbol], start=start, end=end)
    report.residual = len([d for d in compare_days(
        daily_aggregate(after), daily_aggregate(source_frame),
        tol=tol, min_coverage=min_coverage, min_bars=min_bars,
    ) if d.status == "disagree"])
    return report


def _render(report: SymbolReport, *, headline: bool = True) -> None:
    """Human-readable per-symbol report."""
    ratios = report.ratios()
    disagreed = report.disagreed
    if headline:
        factor = ""
        if ratios:
            ordered = sorted(ratios)
            median = ordered[len(ordered) // 2]
            low, high = ordered[0], ordered[-1]
            # A re-based series shows one factor across every day; a bad print
            # shows one day at a factor nothing else shares.
            factor = (f" | uniform factor {median:.4f}"
                      if median and (high - low) / median < 0.02
                      else f" | factors {low:.3f}..{high:.3f}")
        print(f"{report.symbol}: {report.compared} days compared, "
              f"{len(disagreed)} disagree{factor}")
        by_status = {s: sum(1 for d in report.days if d.status == s)
                     for s in ("agree", "disagree", "lake_only", "source_only")}
        print("   " + "  ".join(f"{k}={v}" for k, v in by_status.items()))
    for day in disagreed[:10]:
        mark = "rewrite" if day.rewrite else "KEEP"
        # Volume is the discriminator between the two causes: a split re-bases
        # price and volume by opposite factors, a wrong security does neither.
        vol = f"vol x{day.volume_ratio:.2f}" if day.volume_ratio else "vol -"
        print(f"   {day.day}  lake {day.lake_close:>10.2f}  source "
              f"{day.source_close:>10.2f}  x{day.ratio:.4f}  {vol:>10s}  "
              f"bars {day.lake_bars}->{day.source_bars}  {mark}")
    if len(disagreed) > 10:
        print(f"   ... {len(disagreed) - 10} more disagreeing days")
    if report.skipped:
        print(f"   not rewritten ({len(report.skipped)}):")
        for reason in report.skipped[:5]:
            print(f"     {reason}")
    if report.days_rewritten:
        print(f"   rewrote {report.days_rewritten} days "
              f"({report.rows_written} rows) | residual disagreements: "
              f"{report.residual}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbols", required=True,
                        help="Comma-separated symbols to reconcile")
    parser.add_argument("--broker", default="upstox",
                        help="Broker whose bars are the reference (default: "
                             "upstox, which serves the full 09:15-15:29 session)")
    parser.add_argument("--from", dest="start", default=None,
                        help="Range start (default: the symbol's first stored day)")
    parser.add_argument("--to", dest="end", default=None,
                        help="Range end (default: the symbol's last stored day)")
    parser.add_argument("--tol", type=float, default=_DEFAULT_TOL,
                        help="Relative high-close difference that counts as a "
                             f"disagreement (default: {_DEFAULT_TOL})")
    parser.add_argument("--min-coverage", type=float, default=_DEFAULT_MIN_COVERAGE,
                        help="Bars the broker must return, as a share of the "
                             f"lake's, before a day is rewritten "
                             f"(default: {_DEFAULT_MIN_COVERAGE})")
    parser.add_argument("--min-bars", type=int, default=_DEFAULT_MIN_BARS,
                        help=f"Absolute bar floor for a rewrite "
                             f"(default: {_DEFAULT_MIN_BARS})")
    parser.add_argument("--window-days", type=int, default=_DEFAULT_WINDOW_DAYS,
                        help="Days per broker request (default: "
                             f"{_DEFAULT_WINDOW_DAYS})")
    parser.add_argument("--apply", action="store_true",
                        help="Write the corrections (default: dry run)")
    parser.add_argument("--data-root", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")
    from tradex_trading.config.env import load_env_file
    from tradex_trading.runtime.live import build_broker_from_env

    load_env_file(str(ROOT / ".env.local"))
    store = ParquetStorage(Path(args.data_root) if args.data_root else Path(datalake_root()))
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    broker = build_broker_from_env(args.broker)
    broker.connect()

    start, end = (datetime.fromisoformat(args.start) if args.start else None,
                  datetime.fromisoformat(args.end) if args.end else None)
    reports: list[SymbolReport] = []
    for symbol in symbols:
        span = _lake_span(store, symbol)
        if span is None:
            print(f"{symbol}: nothing stored")
            continue
        winner = (start or span[0], end or span[1])
        report = reconcile_symbol(
            store, broker, symbol, start=winner[0], end=winner[1], tol=args.tol,
            min_coverage=args.min_coverage, min_bars=args.min_bars,
            window_days=args.window_days, apply=args.apply,
        )
        reports.append(report)
        if not args.json:
            _render(report)

    if args.apply:
        unfixed = sum(r.residual for r in reports) + sum(len(r.skipped) for r in reports)
        print(f"\n{'CONFIRMED' if not unfixed else 'INCOMPLETE'}: "
              f"{sum(r.rows_written for r in reports)} rows written, "
              f"{sum(r.residual for r in reports)} residual disagreements, "
              f"{sum(len(r.skipped) for r in reports)} days not rewritten")
    else:
        pending = sum(len([d for d in r.days if d.rewrite]) for r in reports)
        print(f"\ndry run: {pending} day(s) would be rewritten — pass --apply")
    if args.json:
        print(json.dumps([
            {"symbol": r.symbol, "compared": r.compared,
             "disagreed": len(r.disagreed), "days_rewritten": r.days_rewritten,
             "rows_written": r.rows_written, "residual": r.residual,
             "skipped": r.skipped,
             "days": [d.__dict__ | {"ratio": d.ratio} for d in r.disagreed]}
            for r in reports
        ], indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
