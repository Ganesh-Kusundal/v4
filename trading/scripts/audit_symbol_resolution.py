#!/usr/bin/env python3
"""Audit that every lake symbol is the security the universe says it is.

Two questions, because they fail in different places:

1. **Resolution** — does ``NSE:<symbol>`` resolve to the security the universe
   CSV names? Checked against whatever *identity* the master actually carries:
   Upstox publishes an ISIN per row, so the check is exact; Dhan's master has
   no ISIN, so the check is that the winning row is a cash-equity series
   (``EQ``/``BE``). A symbol resolving to another security is the CHOLAFIN bug —
   an equity request served by a debenture that never trades, which reads
   downstream as "this broker has no history for the symbol".

2. **Plausibility** — does the lake's *stored* history look like that security?
   Resolution can be correct today while the bars are wrong anyway: a vendor
   re-bases its history when a security splits, or a broker prints garbage for
   one day, and neither is visible to a gap detector (the bars exist, they are
   just the wrong bars). Judged on turnover, no-trade minutes and single-day
   jumps — none of which a debenture, warrant or bond produces in equity size.

The resolution half is offline: it reads the dated master cache the trading
layer already keeps in ``runtime/`` and runs the *production* loader against it
(``load_instruments``), so what is audited is the resolution the live system
actually performs, not a re-implementation of it. ``--verify-source N`` adds a
network step for the N worst single-day jumps: fetch those days from every
broker and compare against the lake, which is what separates "a real move" from
"a bad print" from "the stored series is at a different level".

Usage::

    python trading/scripts/audit_symbol_resolution.py
    python trading/scripts/audit_symbol_resolution.py --verify-source 5 --json
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))

from tradex_brokers.common.instruments import build_instrument_from_row  # noqa: E402
from tradex_brokers.dhan.adapter import DhanBroker  # noqa: E402
from tradex_brokers.dhan.master import parse_dhan_master  # noqa: E402
from tradex_brokers.upstox.adapter import UpstoxBroker  # noqa: E402
from tradex_brokers.upstox.master import parse_upstox_master  # noqa: E402
from tradex_domain.instruments import Equity  # noqa: E402

from tradex_trading.datalake.paths import datalake_root  # noqa: E402

#: Series codes that mean "the exchange's equity series" in a cash segment.
_CASH_EQUITY_SERIES = frozenset({"EQ", "BE"})

#: A Nifty-500 equity clears far more than this per day; a debenture clears
#: almost nothing. Deliberately loose — this is a debt detector, not a
#: liquidity screen (the lake's thinnest name still turns over ~Rs 4 crore).
_DEFAULT_MIN_TURNOVER = 1e7

#: Single-day close-to-close moves beyond this are past NSE's widest circuit
#: band, so they are a corporate action or a bad print rather than a price move.
_DEFAULT_JUMP = 0.25


def _latest_master(masters_dir: Path, prefix: str) -> Path | None:
    """Newest dated master cache file for *prefix*, or None."""
    found = sorted(masters_dir.glob(f"{prefix}-instruments-*.json"))
    return found[-1] if found else None


def _read_master(path: Path) -> bytes:
    """Raw master bytes (the cache stores the download verbatim, maybe gzipped)."""
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


def _contested(rows: list[dict[str, Any]], key_name: str) -> dict[str, list[str]]:
    """Symbols a single master lists under more than one provider key.

    The domain keys cash equities by *trading symbol*, so two rows for one
    symbol collapse onto one instrument and the loader's claim priority decides
    which key owns it. Anything listed here is worth a look: it is the shape the
    CHOLAFIN bug had.
    """
    claims: dict[str, list[str]] = {}
    for row in rows:
        iid = build_instrument_from_row(row).instrument_id
        claims.setdefault(str(iid), []).append(str(row.get(key_name, "")))
    return {iid: keys for iid, keys in claims.items() if len(set(keys)) > 1}


def _upstox_resolution(raw: bytes, symbols: dict[str, str]) -> dict[str, Any]:
    """Resolve every universe symbol through the Upstox master; check the ISIN."""
    isin_by_key = {
        str(row.get("instrument_key")): str(row.get("isin") or "").upper()
        for row in json.loads(raw.decode("utf-8"))
    }
    rows = parse_upstox_master(raw, strict=False)
    broker = UpstoxBroker(transport=None)
    broker.load_instruments(rows)
    wrong, unresolved = [], []
    for symbol, isin in sorted(symbols.items()):
        key = broker.registry.provider_key(Equity.of("NSE", symbol).instrument_id)
        if key is None:
            unresolved.append(symbol)
        elif isin_by_key.get(key, "") != isin:
            wrong.append({"symbol": symbol, "expected_isin": isin, "key": key,
                          "key_isin": isin_by_key.get(key, "")})
    return {
        "rows": len(rows),
        "unresolved": unresolved,
        "wrong_isin": wrong,
        "contested": _contested(rows, "key"),
    }


def _dhan_resolution(raw: bytes, symbols: dict[str, str]) -> dict[str, Any]:
    """Resolve every universe symbol through the Dhan master; check the series.

    Dhan's master carries no ISIN, so the strictest available identity check is
    that the winning row is a cash-equity series.
    """
    series_by_id: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))):
        if row.get("SEM_EXM_EXCH_ID") == "NSE" and row.get("SEM_SEGMENT") == "E":
            series_by_id[str(row.get("SEM_SMST_SECURITY_ID"))] = str(
                row.get("SEM_SERIES") or ""
            ).upper()
    rows = parse_dhan_master(raw, strict=False)
    broker = DhanBroker(transport=None)
    broker.load_instruments(rows)
    wrong, unresolved = [], []
    for symbol in sorted(symbols):
        key = broker.registry.provider_key(Equity.of("NSE", symbol).instrument_id)
        if key is None:
            unresolved.append(symbol)
            continue
        series = series_by_id.get(str(key).split(":", 1)[-1], "")
        if series not in _CASH_EQUITY_SERIES:
            wrong.append({"symbol": symbol, "key": key, "series": series or "?"})
    return {
        "rows": len(rows),
        "unresolved": unresolved,
        "wrong_series": wrong,
        "contested": _contested(rows, "key"),
    }


def _lake_plausibility(store: Path, universe: str, *, min_turnover: float,
                       jump: float, holidays: frozenset | set,
                       ) -> dict[str, Any]:
    """Per-symbol liquidity and jump metrics over the stored lake.

    Measured on 09:16-15:14, the window both brokers can serve: outside it the
    session edges are an intermittent stamp (09:15) and a carry-forward
    convention (15:15-15:28), and counting those would measure the convention
    rather than the symbol.
    """
    import duckdb

    con = duckdb.connect()
    df = con.execute(
        """
        WITH d AS (
          SELECT symbol, CAST(date_trunc('day', timestamp) AS DATE) AS day,
                 max(close) AS dc, sum(volume) AS vol, count(*) AS bars,
                 sum(CASE WHEN volume = 0 THEN 1 ELSE 0 END) AS zerovol
          FROM read_parquet(? , hive_partitioning=true)
          WHERE CAST(timestamp AS TIME) BETWEEN TIME '09:16' AND TIME '15:14'
            AND CAST(timestamp AS DATE) NOT IN (SELECT unnest(?))
          GROUP BY 1, 2
        ),
        lagged AS (
          SELECT symbol, day, dc, vol, bars, zerovol,
                 lag(dc) OVER (PARTITION BY symbol ORDER BY day) AS prev,
                 lag(day) OVER (PARTITION BY symbol ORDER BY day) AS prev_day
          FROM d
        )
        SELECT symbol,
               count(*) AS days,
               median(dc * vol) AS med_turnover,
               median(dc) AS med_close,
               sum(zerovol) * 1.0 / nullif(sum(bars), 0) AS zerovol_share,
               max(abs(ln(dc / prev))) AS max_jump,
               arg_max(day, abs(ln(dc / prev))) AS jump_day,
               arg_max(prev_day, abs(ln(dc / prev))) AS jump_prev_day
        FROM lagged WHERE prev IS NOT NULL GROUP BY symbol
        """,
        [str(store / "**" / "*.parquet"), sorted(holidays)],
    ).df()
    thin = df[df.med_turnover < min_turnover]
    jumpy = df[df.max_jump > jump].sort_values("max_jump", ascending=False)
    return {
        "symbols": int(len(df)),
        "min_turnover": float(df.med_turnover.min()),
        "median_turnover": float(df.med_turnover.median()),
        "thin": thin[["symbol", "med_close", "med_turnover", "zerovol_share"]]
        .round(4).to_dict("records"),
        "jumps": jumpy[["symbol", "jump_day", "jump_prev_day", "max_jump",
                        "med_close", "med_turnover", "zerovol_share"]]
        .round(4).to_dict("records"),
        "thin_turnover_floor": min_turnover,
        "jump_threshold": jump,
    }


def _load_universe(path: Path) -> dict[str, str]:
    """Universe CSV -> ``{symbol: ISIN}``."""
    with open(path, newline="", encoding="utf-8") as fh:
        return {
            row["Symbol"].strip().upper(): row["ISIN Code"].strip().upper()
            for row in csv.DictReader(fh)
        }


def leaf(value: float | None) -> str:
    """Format a possibly-absent lake close for the cross-check table."""
    return f"{value:.2f}" if value is not None else "-"


def _agrees(a: float, b: float) -> bool:
    """Same instrument, one close: allow a tick of slack, not half a price."""
    return abs(a - b) <= max(0.01, 0.005 * max(abs(a), abs(b)))


def _verdict(lake: float | None, per_broker: list[tuple[str, float | None]]) -> str:
    """Classify one day: is the lake's bar the security's, and whose is it?

    Three outcomes matter. Brokers agreeing *and* the lake agreeing is a plain
    move. Brokers agreeing against the lake means the stored series is at a
    different level — a re-based or unadjusted history. Brokers disagreeing
    means a bad print exists somewhere, and naming which value the lake took
    says whether the lake inherited it.
    """
    quoted = [(name, high) for name, high in per_broker if high is not None]
    if lake is None or not quoted:
        return ""
    reference = quoted[0][1]
    split = [name for name, high in quoted[1:] if not _agrees(high, reference)]
    if split:
        matches = [name for name, high in quoted if _agrees(high, lake)]
        return f"lake == {matches[0]}" if matches else "LAKE MATCHES NEITHER"
    if _agrees(lake, reference):
        return ""
    return f"LAKE != BOTH ({lake / reference:.2f}x)"


def _verify_against_source(cases: list[dict[str, Any]], store: Path, limit: int) -> None:
    """Fetch the worst jump days from every broker and compare with the lake.

    This is what separates the three explanations a jump can have: a real move
    (broker and lake agree, neighbours agree), a bad print (one broker disagrees
    and the neighbours disagree with the lake), or a re-based series (the whole
    stored history sits at a different level than any broker now reports).
    """
    from datetime import date, datetime

    import duckdb

    from tradex_trading.config.env import load_env_file
    from tradex_trading.runtime.live import build_broker_from_env

    load_env_file(str(ROOT / ".env.local"))
    con = duckdb.connect()
    brokers: dict[str, Any] = {}
    for name in ("upstox", "dhan"):
        try:
            broker = build_broker_from_env(name)
            broker.connect()
            brokers[name] = broker
        except Exception as exc:  # noqa: BLE001 — audit reports, never raises
            print(f"  [{name} unavailable: {exc}]", flush=True)

    print("\nsource cross-check (lake vs each broker, both sides of the jump)")
    print(f"  {'symbol':11s} {'day':11s} {'lake':>10s} " +
          " ".join(f"{n:>10s}" for n in brokers) + "   verdict")
    for case in cases[:limit]:
        symbol = str(case["symbol"])
        for day, tag in ((case["jump_prev_day"], "pre "), (case["jump_day"], "jump")):
            day = day.date() if isinstance(day, datetime) else date.fromisoformat(str(day))
            lake = con.execute(
                """SELECT round(max(close), 2) FROM read_parquet(?, hive_partitioning=true)
                   WHERE symbol = ? AND CAST(timestamp AS DATE) = ?""",
                [str(store / "**" / "*.parquet"), symbol, day],
            ).fetchone()
            cells, per_broker = [], []
            for name, broker in brokers.items():
                start = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=15)
                end = start.replace(hour=15, minute=35)
                series = broker.history(Equity.of("NSE", symbol), "1m", start, end)
                closes = [c.ohlc.close.value for c in series.candles]
                high = float(max(closes)) if closes else None
                per_broker.append((name, high))
                cells.append(f"{high:>10.2f}" if high is not None else f"{'-':>10s}")
            lake_close = float(lake[0]) if lake and lake[0] is not None else None
            print(f"  {symbol:11s} {day!s:11s} {leaf(lake_close):>10s} "
                  + " ".join(cells) + f"   {tag} "
                  + _verdict(lake_close, per_broker))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--universe", default="nifty500")
    parser.add_argument("--root", default=None,
                        help="Lake root to read (default: the lake ``serve`` "
                             "itself reads, via datalake_root())")
    parser.add_argument("--masters-dir", default=None,
                        help="Dated master cache dir (default: <repo>/runtime)")
    parser.add_argument("--min-turnover", type=float, default=_DEFAULT_MIN_TURNOVER,
                        help="Median daily turnover below which a symbol is "
                             "debt-like (default: 1e7)")
    parser.add_argument("--jump", type=float, default=_DEFAULT_JUMP,
                        help="Single-day |log| move worth inspecting (default: 0.25)")
    parser.add_argument("--verify-source", type=int, default=0, metavar="N",
                        help="Cross-check the N worst jumps against every broker")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    logging.disable(logging.WARNING)  # the loader's contested-symbol warning is
    # reported structurally below; duplicating it on stderr only adds noise.
    from tradex_domain.market_calendar import NSE_HOLIDAYS_2026

    # The lake root, not its parent: globbing ``<data>/**/*.parquet`` would also
    # read anything else parked under ``data/`` (a repair backup, for one) and
    # silently double-count a symbol — which is exactly what it did once.
    store = Path(args.root) if args.root else Path(datalake_root())
    masters = Path(args.masters_dir) if args.masters_dir else ROOT / "runtime"
    symbols = _load_universe(ROOT / "Dependencies" / f"{args.universe}_list.csv")

    report: dict[str, Any] = {"universe": args.universe, "symbols": len(symbols)}
    for name, prefix, check in (
        ("upstox", "upstox", _upstox_resolution),
        ("dhan", "dhan", _dhan_resolution),
    ):
        path = _latest_master(masters, prefix)
        if path is None:
            report[name] = {"master": None}
            continue
        raw = _read_master(path)
        entry = check(raw, symbols)
        entry["master"] = path.name
        report[name] = entry

    report["lake"] = _lake_plausibility(
        store, args.universe, min_turnover=args.min_turnover, jump=args.jump,
        holidays=NSE_HOLIDAYS_2026,
    )

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print(f"resolution audit — {args.universe} ({len(symbols)} symbols)")
    for name in ("upstox", "dhan"):
        entry = report[name]
        if entry.get("master") is None:
            print(f"  {name:7s}: no cached master under {masters}")
            continue
        wrong = entry.get("wrong_isin") or entry.get("wrong_series") or []
        ident = "ISIN" if name == "upstox" else "series"
        print(f"  {name:7s}: {entry['rows']:,} rows | {len(entry['unresolved'])} "
              f"unresolved | {len(wrong)} resolving to the wrong {ident} | "
              f"{len(entry['contested'])} contested symbol(s)")
        for item in wrong[:10]:
            print(f"           MISMATCH {item}")
    lake = report["lake"]
    print(f"\nlake plausibility — {lake['symbols']} symbols")
    print(f"  median daily turnover {lake['median_turnover'] / 1e7:,.1f} crore, "
          f"lowest {lake['min_turnover'] / 1e7:,.1f} crore")
    print(f"  debt-like (below {lake['thin_turnover_floor'] / 1e7:,.1f} crore): "
          f"{len(lake['thin'])}")
    for item in lake["thin"][:10]:
        print(f"           {item}")
    print(f"  single-day jumps beyond {lake['jump_threshold']:.0%}: "
          f"{len(lake['jumps'])}")
    for item in lake["jumps"][:10]:
        print(f"           {item['symbol']:11s} {str(item['jump_day'])[:10]} "
              f"{item['max_jump']:.3f} close {item['med_close']:.0f}")
    if args.verify_source:
        _verify_against_source(lake["jumps"], store, args.verify_source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
