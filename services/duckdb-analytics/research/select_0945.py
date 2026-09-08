"""09:45 selection pipeline for day trading — screener + realized outcome.

At the 09:45 decision point we screen the whole universe with the tuned
top-gainer criteria (gap / rel-volume / opening drive / RS / ADX / ATR),
then attach each signal's *realized* return over the 09:45->15:15 window so
you can see the gap between "looked good at 09:45" and "actually ran".

Usage::

    python select_0945.py --as-of "2026-09-08 09:45:00" --top 10
    python select_0945.py --as-of "2026-09-07 09:45:00" --strict --top 15

Point-in-time safe: the screener bound is the 09:45 timestamp, and the
realized-return join only reads bars up to 15:15 on the SAME day — nothing
from the future leaks in.
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


def select_and_outcome(
    as_of: str,
    *,
    top: int = 15,
    strict: bool = False,
    min_open: float = 100.0,
) -> None:
    day = as_of.split(" ")[0]
    cfg = default_config_for(_REPO)
    cat = DuckDBCatalog(cfg)
    svc = QueryService(cat, cfg)
    try:
        q = scan_screener(as_of, strict=strict, min_open=min_open)
        sig = svc.execute(q.sql, require_complete=True)
        if not sig.rows:
            print(f"no signals at {as_of}")
            return

        # Realized 09:45 -> 15:15 return for every symbol in the universe.
        # first(open) @>=09:45, last(close) @<=15:15, same day only.
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

        # screener columns: symbol, day, open_px, gap_pct, pre30_ret,
        # v30x, rs_score, atr, atr_pct
        scored = []
        for row in sig.rows:
            sym = row[0]
            o945, c1515 = out.get(sym, (None, None))
            ret = (
                (c1515 / o945 - 1) * 100
                if o945 and c1515 and o945 > 0
                else None
            )
            scored.append((sym, row, ret))

        scored.sort(key=lambda t: (t[2] is None, -(t[2] or 0)))
        n_sig = len(scored)
        hit = sum(1 for _, _, r in scored if r is not None and r > 0)
        avg = (
            sum(r for _, _, r in scored if r is not None) / n_sig
            if n_sig else 0.0
        )

        print(f"== {as_of}  ({'strict' if strict else 'loose'}) ==")
        print(f"{q.description}")
        print(f"signals: {n_sig}  |  win-rate 09:45->15:15: {hit}/{n_sig} "
              f"({hit / max(n_sig, 1):.0%})  |  avg return: {avg:+.2f}%")
        print(f"{'symbol':<14}{'open@09:45':>11}{'close@15:15':>13}"
              f"{'v30x':>7}{'RS':>7}{'realized%':>11}")
        for sym, row, ret in scored[:top]:
            o945, c1515 = out.get(sym, (None, None))
            v30x, rs = row[5], row[6]
            ret_s = (
                f"{'+' if ret and ret > 0 else ''}{ret:.2f}"
                if ret is not None else "n/a"
            )
            print(f"{sym:<14}"
                  f"{('?' if o945 is None else f'{o945:.1f}'):>11}"
                  f"{('?' if c1515 is None else f'{c1515:.1f}'):>13}"
                  f"{v30x:>7.1f}{rs:>7.2f}{ret_s:>11}")
    finally:
        cat.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--as-of", required=True, help="'YYYY-MM-DD 09:45:00'")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--min-open", type=float, default=100.0)
    args = ap.parse_args(argv)
    select_and_outcome(
        args.as_of, top=args.top, strict=args.strict, min_open=args.min_open
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())