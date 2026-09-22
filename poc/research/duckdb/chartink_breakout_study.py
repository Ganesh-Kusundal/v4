"""Chartink-style prior-day-structure breakout scan, replicated on our lake.

Decodes the pasted scanner expression into executable conditions:

  c1  prev-day (H+L+O+C)/4 > (O+C)/2          close sat in lower half of range
  c2  today's running volume > 250,000
  c3  latest close > yesterday's Heikin-Ashi high
  c4  latest 30m close > 7-day mean daily close   [proxy for ^2264 'val']
  c5  5m close > rolling-30x30m high  OR  < rolling-30x30m low
      (break of the prior 1.5 sessions' 30-minute structure)
  c6  today's running (cumulative) volume > 20-day mean daily volume
  c7  30m close > ADX(14) computed on 30m bars
  c8  latest close > daily SMA(20)                [proxy for ^15728 trend = 1]

^2264 and ^15728 are private custom studies — their formulas are not
observable, so c4/c8 use transparent proxies (7-day mean close, SMA20
trend state). Swap them in ``PROXIES`` when the real formulas are known.

Usage::

    .venv/bin/python services/duckdb-analytics/research/chartink_breakout_study.py \
        [--as-of "2026-09-10 11:12:00"] [--top 30]

Backtest/CSV mode::

    .venv/bin/python services/duckdb-analytics/research/chartink_breakout_study.py \
        --backtest [n-days] --csv out.csv

    Replays the last n-days completed sessions with as-of set to that day's
    close, compares the old c6 (last 30m bucket volume > ADV) against the
    new cumulative-volume c6, and reports next-day outcomes per variant.
    --csv writes the day-by-day hit history for both c6 variants.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[3]
SESSION_OPEN = "09:15:00"
SESSION_CLOSE = "15:30:00"
GLOB = str(REPO / "data" / "ohlcv" / "**" / "data.parquet")

# Tick source for the intraday queries. Backtest mode repoints this at an
# in-memory temp table (t_ticks) covering the replay window so per-day
# queries don't rescan the parquet lake 4x per replayed session.
_SRC = f"read_parquet('{GLOB}', hive_partitioning=true)"

# Trading-day arithmetic helper: keep date math in datetime64[ns] (D unit)
# instead of Timedelta(days=...) on bare integers, which NumPy now deprecates.
def _shift_day(ts, days: int) -> str:
    return str((pd.Timestamp(ts) + np.timedelta64(days, "D")).date())





def _bucket_sql(minutes: int) -> str:
    return (
        f"CAST(timestamp AS DATE) + (FLOOR(datediff('minute', "
        f"TIME '{SESSION_OPEN}', CAST(timestamp AS TIME)) / {minutes}) "
        f"* INTERVAL '{minutes} minutes')"
    )


def heikin_ashi(daily: pd.DataFrame) -> pd.DataFrame:
    """HA series per symbol; returns frame with ha_high (input sorted by d)."""
    out = []
    for _, g in daily.groupby("symbol", sort=False):
        o = g["o"].to_numpy(float)
        h = g["h"].to_numpy(float)
        l = g["l"].to_numpy(float)
        c = g["c"].to_numpy(float)
        ha_c = (o + h + l + c) / 4.0
        ha_o = np.empty(len(g))
        ha_o[0] = (o[0] + c[0]) / 2.0
        for i in range(1, len(g)):
            ha_o[i] = (ha_o[i - 1] + ha_c[i - 1]) / 2.0
        ha_h = np.maximum.reduce([h, ha_o, ha_c])
        out.append(pd.DataFrame({
            "symbol": g["symbol"].to_numpy(), "d": g["d"].to_numpy(),
            "ha_high": ha_h,
        }))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def adx14(frame: pd.DataFrame) -> pd.DataFrame:
    """Wilder ADX(14) per symbol on OHLC bars (ewm alpha=1/14 == RMA)."""
    n = 14
    rows = []
    for sym, g in frame.groupby("symbol", sort=False):
        h, l, c = g["high"].to_numpy(float), g["low"].to_numpy(float), g["close"].to_numpy(float)
        if len(g) < 2 * n + 2:
            continue
        up = np.diff(h, prepend=h[0])
        dn = -np.diff(l, prepend=l[0])
        plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
        minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
        tr = np.maximum.reduce([
            h[1:] - l[1:],
            np.abs(h[1:] - c[:-1]),
            np.abs(l[1:] - c[:-1]),
        ])
        tr = np.concatenate([[h[0] - l[0]], tr])
        rma = lambda x: pd.Series(x).ewm(alpha=1.0 / n, adjust=False).mean().to_numpy()
        atr = rma(tr)
        pdi = 100.0 * rma(plus_dm) / np.where(atr == 0, np.nan, atr)
        mdi = 100.0 * rma(minus_dm) / np.where(atr == 0, np.nan, atr)
        dx = 100.0 * np.abs(pdi - mdi) / np.where((pdi + mdi) == 0, np.nan, pdi + mdi)
        rows.append({"symbol": sym, "adx": rma(dx)[-1], "close": c[-1]})
    return pd.DataFrame(rows)


def _run_scan(con, as_of: str, daily: pd.DataFrame | None = None) -> tuple[str, pd.DataFrame]:
    """Evaluate all 8 conditions per symbol as of an intraday timestamp.

    Returns (day, frame) with per-symbol condition columns, rrv and the last
    30m-bucket volume (``m30_vol``) for the legacy-c6 variant. All lake reads
    are clamped to ``timestamp <= as_of`` so historical replays see exactly
    what a live run at that moment would have seen. ``daily`` may carry a
    pre-fetched 200-day aggregate (all rows up to a later session are
    filtered out per-day anyway), which lets the backtest reuse one scan.
    """
    as_of = str(as_of)
    day = str(pd.Timestamp(as_of).date())
    hist_start = _shift_day(as_of, -200)

    if daily is None:
        daily = con.execute(f"""
        SELECT symbol, CAST(timestamp AS DATE) AS d,
            first(open ORDER BY timestamp) AS o, max(high) AS h,
            min(low) AS l, last(close ORDER BY timestamp) AS c,
            sum(volume) AS vol
        FROM read_parquet('{GLOB}', hive_partitioning=true)
        WHERE CAST(timestamp AS TIME) >= TIME '{SESSION_OPEN}'
          AND CAST(timestamp AS TIME) <= TIME '{SESSION_CLOSE}'
          AND CAST(timestamp AS DATE) >= DATE '{hist_start}'
          AND timestamp <= TIMESTAMP '{as_of}'
        GROUP BY symbol, CAST(timestamp AS DATE)
        ORDER BY symbol, d
    """).fetchdf()

    today_agg = con.execute(f"""
        SELECT symbol, sum(volume) AS vol_today, last(close ORDER BY timestamp) AS last_close,
            max(CAST(timestamp AS TIME)) AS last_t
        FROM {_SRC}
        WHERE CAST(timestamp AS DATE) = DATE '{day}'
          AND CAST(timestamp AS TIME) >= TIME '{SESSION_OPEN}'
          AND timestamp <= TIMESTAMP '{as_of}'
        GROUP BY symbol
    """).fetchdf()

    m30 = con.execute(f"""
        SELECT symbol, {_bucket_sql(30)} AS bucket,
            first(open ORDER BY timestamp) AS open, max(high) AS high,
            min(low) AS low, last(close ORDER BY timestamp) AS close,
            sum(volume) AS vol
        FROM {_SRC}
        WHERE timestamp <= TIMESTAMP '{as_of}'
          AND CAST(timestamp AS DATE) >= DATE '{_shift_day(day, -14)}'
          AND CAST(timestamp AS TIME) >= TIME '{SESSION_OPEN}'
          AND CAST(timestamp AS TIME) < TIME '{SESSION_CLOSE}'
        GROUP BY symbol, bucket ORDER BY symbol, bucket
    """).fetchdf()

    m5 = con.execute(f"""
        SELECT symbol, {_bucket_sql(5)} AS bucket,
            last(close ORDER BY timestamp) AS close
        FROM {_SRC}
        WHERE CAST(timestamp AS DATE) = DATE '{day}'
          AND CAST(timestamp AS TIME) >= TIME '{SESSION_OPEN}'
          AND CAST(timestamp AS TIME) < TIME '{SESSION_CLOSE}'
          AND timestamp <= TIMESTAMP '{as_of}'
        GROUP BY symbol, bucket ORDER BY symbol, bucket
    """).fetchdf()

    daily["d"] = pd.to_datetime(daily["d"])
    prev = daily[daily["d"] < pd.Timestamp(day)].groupby("symbol").tail(1)
    prev = prev.rename(columns={"o": "o_y", "h": "h_y", "l": "l_y", "c": "c_y"})
    ha = heikin_ashi(daily[daily["d"] < pd.Timestamp(day)])
    ha_y = ha.groupby("symbol").tail(1)[["symbol", "ha_high"]]

    # c4/c8 proxies: 7-day mean close and SMA20 as of yesterday
    prox = daily[daily["d"] < pd.Timestamp(day)].groupby("symbol").apply(
        lambda g: pd.Series({
            "mean7": g["c"].tail(7).mean(), "sma20": g["c"].tail(20).mean(),
        }), include_groups=False).reset_index()
    avg_vol20 = daily[daily["d"] < pd.Timestamp(day)].groupby("symbol").apply(
        lambda g: g["vol"].tail(20).mean(), include_groups=False).rename("avg_vol20").reset_index()

    # c5: rolling-30 30m high/low ending yesterday
    today_start = pd.Timestamp(day)
    prior_m30 = m30[m30["bucket"] < today_start]
    struct = prior_m30.groupby("symbol").apply(
        lambda g: pd.Series({
            "hi30": g.sort_values("bucket")["high"].tail(30).max(),
            "lo30": g.sort_values("bucket")["low"].tail(30).min(),
        }), include_groups=False).reset_index()
    last_m5 = m5.groupby("symbol").tail(1)[["symbol", "close"]].rename(columns={"close": "close5"})
    last_m30 = m30.groupby("symbol").tail(1)
    adx_df = adx14(m30)

    f = (today_agg
         .merge(prev[["symbol", "o_y", "h_y", "l_y", "c_y"]], on="symbol", how="left")
         .merge(ha_y, on="symbol", how="left")
         .merge(prox, on="symbol", how="left")
         .merge(avg_vol20, on="symbol", how="left")
         .merge(struct, on="symbol", how="left")
         .merge(last_m5, on="symbol", how="left")
         .merge(last_m30[["symbol", "close", "vol"]].rename(
             columns={"close": "m30_close", "vol": "m30_vol"}), on="symbol", how="left")
         .merge(adx_df.rename(columns={"close": "close_adx"}), on="symbol", how="left"))
    f = f.dropna(subset=["h_y", "ha_high", "hi30", "avg_vol20"])

    f["c1"] = (f["h_y"] + f["l_y"]) / 2.0 > (f["o_y"] + f["c_y"]) / 2.0   # (OHLC)/4 > (OC)/2
    f["c2"] = f["vol_today"] > 250_000
    f["c3"] = f["last_close"] > f["ha_high"]
    f["c4"] = f["m30_close"] > f["mean7"]
    f["c5_breakout"] = f["close5"] > f["hi30"]
    f["c5_breakdown"] = f["close5"] < f["lo30"]
    f["c5"] = f["c5_breakout"] | f["c5_breakdown"]
    f["c6"] = f["vol_today"] > f["avg_vol20"]
    f["rrv"] = f["vol_today"] / f["avg_vol20"]   # relative volume: day pace vs 20-day ADV
    f["c7"] = f["m30_close"] > f["adx"]
    f["c8"] = f["last_close"] > f["sma20"]
    cond_cols = ["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"]

    return day, f


def _print_day(day: str, as_of: str, f: pd.DataFrame, top: int) -> None:
    """Live-scan report: pass rates, hits ranked by rrv, near-misses."""
    cond_cols = ["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"]

    print(f"as-of: {as_of} (day {day})\n")
    print("condition pass rates (of "
          f"{len(f)} symbols with sufficient history):")
    for col in cond_cols:
        print(f"  {col}: {f[col].mean():5.1%}")
    print()

    hits = f[f[cond_cols].all(axis=1)].copy()
    hits["dir"] = np.where(hits["c5_breakout"], "breakout", "breakdown")
    hits = hits.sort_values("rrv", ascending=False)
    print(f"ALL conditions matched: {len(hits)} symbols")
    if len(hits):
        show = hits.head(top)
        print(f"{'symbol':<14}{'last':>10}{'vol_today':>11}{'rrv':>7}"
              f"{'dir':>10}{'ha_high':>10}{'hi30':>10}{'lo30':>10}{'adx':>7}")
        for r in show.itertuples():
            print(f"{r.symbol:<14}{r.last_close:>10.1f}{int(r.vol_today):>11,}"
                  f"{r.rrv:>7.2f}{r.dir:>10}{r.ha_high:>10.1f}{r.hi30:>10.1f}"
                  f"{r.lo30:>10.1f}{r.adx:>7.1f}")
        if len(hits) > len(show):
            print(f"  ... and {len(hits) - len(show)} more")

    # Near-miss diagnostic: best 7/8 symbols and which condition failed.
    f["n_pass"] = f[cond_cols].sum(axis=1)
    near = f[f["n_pass"] >= len(cond_cols) - 1].sort_values(
        ["n_pass", "rrv"], ascending=False)
    if len(near):
        print(f"\nnear-misses (>= {len(cond_cols) - 1}/8 conditions):")
        print(f"{'symbol':<14}{'passed':>8}  failed")
        for r in near.head(12).itertuples():
            failed = [c for c in cond_cols if not getattr(r, c)]
            print(f"{r.symbol:<14}{int(r.n_pass):>6}/8  {','.join(failed)}")


def _backtest(n_days: int, csv_path: str | None) -> None:
    """Replay completed sessions; compare legacy vs cumulative-volume c6."""
    cond_new = ["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"]
    cond_old = ["c1", "c2", "c3", "c4", "c5", "c6_legacy", "c7", "c8"]

    con = duckdb.connect()
    sessions = [str(r[0]) for r in con.execute(f"""
        SELECT DISTINCT CAST(timestamp AS DATE) AS d
        FROM read_parquet('{GLOB}', hive_partitioning=true)
        ORDER BY d
    """).fetchall()]
    if len(sessions) < n_days + 1:
        n_days = len(sessions) - 1
    days = sessions[-(n_days + 1):-1]
    nexts = sessions[-n_days:]

    # One 200-day daily aggregate shared by every replayed day (rows for
    # later sessions are filtered out per-day inside _run_scan).
    shared_daily = con.execute(f"""
        SELECT symbol, CAST(timestamp AS DATE) AS d,
            first(open ORDER BY timestamp) AS o, max(high) AS h,
            min(low) AS l, last(close ORDER BY timestamp) AS c,
            sum(volume) AS vol
        FROM read_parquet('{GLOB}', hive_partitioning=true)
        WHERE CAST(timestamp AS TIME) >= TIME '{SESSION_OPEN}'
          AND CAST(timestamp AS TIME) <= TIME '{SESSION_CLOSE}'
          AND CAST(timestamp AS DATE) >= DATE '{_shift_day(days[0], -200)}'
        GROUP BY symbol, CAST(timestamp AS DATE)
        ORDER BY symbol, d
    """).fetchdf()

    # Cache the replay window's intraday ticks in memory; per-day queries
    # below hit this table instead of rescanning the lake.
    global _SRC
    prev_src = _SRC
    _SRC = "t_ticks"
    try:
        con.execute(f"""
            CREATE TEMP TABLE t_ticks AS
            SELECT timestamp, symbol, open, high, low, close, volume
            FROM read_parquet('{GLOB}', hive_partitioning=true)
            WHERE timestamp >= TIMESTAMP '{_shift_day(days[0], -14)}'
              AND CAST(timestamp AS TIME) >= TIME '{SESSION_OPEN}'
              AND CAST(timestamp AS TIME) <= TIME '{SESSION_CLOSE}'
        """)

        summary: list[dict] = []
        csv_rows: list[dict] = []
        for day, nxt in zip(days, nexts):
            _, f = _run_scan(con, f"{day} {SESSION_CLOSE}", daily=shared_daily)
            f["c6_legacy"] = f["m30_vol"] > f["avg_vol20"]
            next_close = con.execute(f"""
                SELECT symbol, last(close ORDER BY timestamp) AS c_next
                FROM {_SRC}
                WHERE CAST(timestamp AS DATE) = DATE '{nxt}'
                  AND CAST(timestamp AS TIME) >= TIME '{SESSION_OPEN}'
                  AND CAST(timestamp AS TIME) <= TIME '{SESSION_CLOSE}'
                GROUP BY symbol
            """).fetchdf()

            per_variant: dict[str, dict] = {}
            for name, cols in (("old", cond_old), ("new", cond_new)):
                hits = f[f[cols].all(axis=1)].merge(next_close, on="symbol", how="left")
                rets = ((hits["c_next"] - hits["last_close"]) / hits["last_close"]).dropna()
                per_variant[name] = {
                    "hits": len(hits),
                    "mean_ret": float(rets.mean()) if len(rets) else float("nan"),
                    "win": float((rets > 0).mean()) if len(rets) else float("nan"),
                }
                for r in hits.itertuples():
                    ret = ((r.c_next - r.last_close) / r.last_close
                           if not pd.isna(r.c_next) else float("nan"))
                    csv_rows.append({
                        "variant": name, "d": day, "next_d": nxt, "symbol": r.symbol,
                        "dir": "breakout" if r.c5_breakout else "breakdown",
                        "last_close": r.last_close, "vol_today": int(r.vol_today),
                        "rrv": round(r.rrv, 3), "adx": round(r.adx, 1),
                        "next_close": r.c_next, "next_ret": ret,
                    })

            summary.append({
                "day": day, "eval": len(f),
                "old_hits": per_variant["old"]["hits"],
                "new_hits": per_variant["new"]["hits"],
                "old_mean_ret": per_variant["old"]["mean_ret"],
                "new_mean_ret": per_variant["new"]["mean_ret"],
                "old_win": per_variant["old"]["win"],
                "new_win": per_variant["new"]["win"],
                "partial": " *" if nxt == sessions[-1] else "",
            })
            print(f"  {day}: eval={len(f)} old={per_variant['old']['hits']} "
                  f"new={per_variant['new']['hits']}", flush=True)
    finally:
        _SRC = prev_src
    con.close()

    def ret_fmt(v: float) -> str:
        return f"{v:>+8.2%}" if v == v else f"{'-':>8}"

    def win_fmt(v: float) -> str:
        return f"{v:>8.0%}" if v == v else f"{'-':>8}"

    print(f"backtest: last {len(days)} completed sessions "
          "(as-of each day's close; * = next-day still running)\n")
    print(f"{'day':<12}{'eval':>6}{'old':>5}{'new':>5}"
          f"{'old_ret':>9}{'new_ret':>9}{'old_win':>9}{'new_win':>9}")
    for s in summary:
        print(f"{s['day']:<12}{s['eval']:>6}{s['old_hits']:>5}{s['new_hits']:>5}"
              f"{ret_fmt(s['old_mean_ret'])}{ret_fmt(s['new_mean_ret'])}"
              f"{win_fmt(s['old_win'])}{win_fmt(s['new_win'])}{s['partial']}")
    for name in ("old", "new"):
        n = sum(s[f"{name}_hits"] for s in summary)
        rets = [s[f"{name}_mean_ret"] for s in summary
                if s[f"{name}_mean_ret"] == s[f"{name}_mean_ret"]]
        wins = [s[f"{name}_win"] for s in summary
                if s[f"{name}_win"] == s[f"{name}_win"]]
        mean = sum(rets) / len(rets) if rets else float("nan")
        wmean = sum(wins) / len(wins) if wins else float("nan")
        print(f"  {name}-c6: {n} hits total, "
              f"mean next-day ret {mean:+.2%}, win rate {wmean:.0%}")

    if csv_path:
        pd.DataFrame(csv_rows).to_csv(csv_path, index=False)
        print(f"\nwrote {len(csv_rows)} hit rows to {csv_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--as-of", default=None, help="'YYYY-MM-DD HH:MM' (default: latest bar)")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--backtest", type=int, default=0, metavar="N",
                    help="replay the last N completed sessions (old vs new c6)")
    ap.add_argument("--csv", default=None,
                    help="write day-by-day hit history CSV (backtest mode)")
    args = ap.parse_args()

    if args.backtest:
        _backtest(args.backtest, args.csv)
        return 0

    con = duckdb.connect()
    as_of = args.as_of or con.execute(
        f"SELECT max(timestamp) FROM read_parquet('{GLOB}', hive_partitioning=true)"
    ).fetchone()[0]
    day, f = _run_scan(con, str(as_of))
    con.close()
    _print_day(day, str(as_of), f, args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
