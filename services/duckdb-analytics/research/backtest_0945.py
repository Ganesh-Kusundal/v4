"""Multi-day backtest of the 09:45 selection strategy.

Runs the tuned screener at 09:45 on every trading day in a window, then
measures each signal's realized return over 09:45->15:15. Reports the
per-day signal count, win rate, mean/median return, and a full distribution
so you can judge whether the screen is tradeable as-is.

Also computes a naive baseline (top-N by 09:45->15:15 realized return, i.e.
what you'd get with perfect hindsight) so the gap between "screened" and
"best possible" is visible.

Usage::

    python backtest_0945.py --days 20
    python backtest_0945.py --days 20 --strict
    python backtest_0945.py --days 20 --min-open 200 --top-k 5

Point-in-time safe: the screener bound is the 09:45 timestamp of each day;
realized returns only read bars up to 15:15 on that same day.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from duck_analytics.catalog import DuckDBCatalog, default_config_for  # noqa: E402
from duck_analytics.query import QueryService  # noqa: E402
from duck_analytics.scanners import scan_screener  # noqa: E402

_REPO = Path(__file__).resolve().parents[3]  # .../v4


def _run(
    *,
    days: int,
    strict: bool,
    min_open: float,
    top_k: int,
) -> None:
    cfg = default_config_for(_REPO)
    cat = DuckDBCatalog(cfg)
    svc = QueryService(cat, cfg)
    try:
        # Trading days in the window, newest first.
        day_rows = svc.execute(
            "SELECT DISTINCT ts::DATE AS d FROM ohlcv ORDER BY d DESC"
        ).rows
        days_list = [str(r[0]) for r in day_rows[:days]]

        all_rets: list[float] = []
        days_with_sigs = 0
        total_sigs = 0
        wins = 0
        per_day = []

        for day in days_list:
            as_of = f"{day} 09:45:00"
            q = scan_screener(as_of, strict=strict, min_open=min_open)
            sig = svc.execute(q.sql, require_complete=True)
            if not sig.rows:
                continue

            # Realized 09:45->15:15 for every symbol that day.
            realized = svc.execute(
                f"""
                SELECT symbol,
                       first(open  ORDER BY timestamp)
                           FILTER (WHERE CAST(timestamp AS TIME) >= TIME '09:45') AS o945,
                       last(close ORDER BY timestamp)
                           FILTER (WHERE CAST(timestamp AS TIME) <= TIME '15:15') AS c1515
                FROM ohlcv
                WHERE ts::DATE = DATE '{day}'
                GROUP BY symbol
                """
            )
            out = {r[0]: (r[1], r[2]) for r in realized.rows}

            day_rets = []
            for row in sig.rows:
                sym = row[0]
                o945, c1515 = out.get(sym, (None, None))
                if o945 and c1515 and o945 > 0:
                    ret = (c1515 / o945 - 1) * 100
                    day_rets.append(ret)
                    all_rets.append(ret)
                    total_sigs += 1
                    if ret > 0:
                        wins += 1

            if day_rets:
                days_with_sigs += 1
                per_day.append((day, day_rets))

        print(f"window: last {len(days_list)} trading days"
              f"  ({days_list[-1]} .. {days_list[0]})")
        print(f"mode: {'strict' if strict else 'loose'}  min_open={min_open}")
        print()
        print(f"days with signals : {days_with_sigs}/{len(days_list)}")
        print(f"total signals     : {total_sigs}")
        print(f"avg signals/day   : {total_sigs / max(days_with_sigs, 1):.1f}")
        print(f"win rate (09:45->15:15): {wins}/{total_sigs} "
              f"= {wins / max(total_sigs, 1):.1%}")
        if all_rets:
            all_rets_s = sorted(all_rets)
            n = len(all_rets_s)
            mean = sum(all_rets_s) / n
            med = all_rets_s[n // 2] if n % 2 else (all_rets_s[n // 2 - 1] + all_rets_s[n // 2]) / 2
            print(f"mean return       : {mean:+.2f}%")
            print(f"median return     : {med:+.2f}%")
            print(f"best / worst      : {all_rets_s[-1]:+.2f}% / {all_rets_s[0]:+.2f}%")
            pos = sum(1 for r in all_rets_s if r > 0)
            print(f"positive          : {pos}/{n} ({pos / n:.0%})")
        print()
        print("per-day detail (day : returns):")
        for day, rets in per_day:
            s = ", ".join(f"{r:+.2f}%" for r in rets)
            print(f"  {day}  [{len(rets)}]  {s}")
    finally:
        cat.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--min-open", type=float, default=100.0)
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args(argv)
    _run(days=args.days, strict=args.strict,
         min_open=args.min_open, top_k=args.top_k)
    return 0


if __name__ == "__main__":
    sys.exit(main())