"""Research: which 09:45-observable features predict the 09:45→15:15 move?

For every symbol-day in the lake we build pre-09:45 features (no look-ahead)
and a forward outcome (09:45→15:15 return). We then test each feature's daily
rank-IC vs the forward move, decile spreads (Q10−Q1), and hit rates for picking
+2%+ movers. Output: a ranked table of features and composite score variants.

Usage:  .venv/bin/python research/scan_0945_forward.py [N_DAYS]
        N_DAYS: use only the N most recent lake sessions (default: all)
"""

from __future__ import annotations

import sys
import threading
from datetime import date

import duckdb
import numpy as np
import pandas as pd

LAKE_GLOB = "data/ohlcv/**/data.parquet"
SESSION_LO, SESSION_HI = "09:15", "15:29"
OR_TO = "09:45"          # scanner cut-off (information available by now)
OUT_FROM = "09:45"       # forward move: OR_TO → this close...
OUT_TO = "15:15"         # ...or exit at window end, whichever first
BASELINE_DAYS = 20

_DUCK_LOCK = threading.Lock()


def _cached(fn):  # tiny memoizer — one connection per process
    memo = {}

    def wrapper(*a):
        if a not in memo:
            memo[a] = fn(*a)
        return memo[a]

    return wrapper


@_cached
def _con():
    con = duckdb.connect()
    con.execute("SET memory_limit='6GB'")
    con.execute("SET threads=4")
    return con


def _query(sql: str, params: list | None = None) -> pd.DataFrame:
    args = params if params is not None else []
    with _DUCK_LOCK:
        try:
            return _con().execute(sql, args).df()
        except duckdb.IOException:
            return _con().execute(sql, args).df()


def lake_days() -> list[date]:
    rows = _query(
        "SELECT DISTINCT timestamp::DATE AS d FROM read_parquet(?, hive_partitioning=true) ORDER BY d",
        [LAKE_GLOB],
    )
    return [ts.date() for ts in rows["d"]]


def build_dataset(days: list[date]) -> pd.DataFrame:
    """One row per symbol-day: pre-09:45 features + forward outcome."""
    day_list = ", ".join(f"DATE '{x}'" for x in days)
    sql = f"""
    WITH px AS (
      SELECT symbol, timestamp::DATE AS day, timestamp,
             open, high, low, close, volume
      FROM read_parquet(?, hive_partitioning=true)
      WHERE timestamp::DATE IN ({day_list})
        AND CAST(timestamp AS TIME) BETWEEN TIME '{SESSION_LO}' AND TIME '{SESSION_HI}'
    ),
    with_parts AS (
      SELECT *,
        CASE WHEN CAST(timestamp AS TIME) <= TIME '{OR_TO}' THEN 'pre' ELSE 'post' END AS part
      FROM px
    ),
    pre AS (
      SELECT symbol, day,
        first(open ORDER BY timestamp) AS open,
        arg_max(close, timestamp) FILTER (WHERE part = 'pre') AS pre_close,
        max(high) FILTER (WHERE part = 'pre') AS pre_high,
        min(low)  FILTER (WHERE part = 'pre') AS pre_low,
        max(high) FILTER (WHERE part = 'pre') AS pre_maxhigh,
        min(low)  FILTER (WHERE part = 'pre') AS pre_minlow,
        sum(volume) FILTER (WHERE part = 'pre') AS pre_vol,
        sum(CASE WHEN part = 'pre' THEN 1 ELSE 0 END) AS pre_bars
      FROM with_parts GROUP BY symbol, day
    ),
    prev_close AS (
      SELECT symbol, day,
        lag(close) OVER (PARTITION BY symbol ORDER BY day) AS pc
      FROM (SELECT symbol, day, arg_max(close, timestamp) AS close
            FROM with_parts GROUP BY symbol, day) t
    ),
    daily AS (
      SELECT symbol, day,
        max(high) - min(low) AS day_range,
        (max(high) - min(low)) / NULLIF(first(open ORDER BY timestamp), 0) AS rel_range,
        sum(volume) AS day_vol
      FROM px GROUP BY symbol, day
    ),
    daily_ranked AS (
      SELECT *,
        avg(rel_range) OVER (PARTITION BY symbol ORDER BY day ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS avg_rel_range,
        avg(day_vol)   OVER (PARTITION BY symbol ORDER BY day ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS avg_day_vol
      FROM daily
    ),
    post AS (
      SELECT symbol, day,
        arg_max(close, timestamp) FILTER (
          WHERE CAST(timestamp AS TIME) <= TIME '{OUT_TO}'
        ) AS out_close,
        arg_max(close, timestamp) FILTER (
          WHERE CAST(timestamp AS TIME) <= TIME '09:50'
        ) AS c_0950
      FROM with_parts GROUP BY symbol, day
    ),
    prev_vwap AS (
      SELECT symbol, day,
        lag(vwap) OVER (PARTITION BY symbol ORDER BY day) AS pvwap
      FROM (SELECT symbol, day,
              sum(close * volume) / NULLIF(sum(volume), 0) AS vwap
            FROM px GROUP BY symbol, day) t
    )
    SELECT p.symbol, p.day,
      -- forward outcomes: 09:45 → 15:15, and the top-gainer window 09:50 → 15:15
      100.0 * (o.out_close / NULLIF(p.pre_close, 0) - 1) AS fwd_pct,
      100.0 * (o.out_close / NULLIF(o.c_0950, 0) - 1) AS win_pct,
      100.0 * (p.open / NULLIF(vw.pvwap, 0) - 1) AS open_vs_pvwap,
      p.pre_close AS px_0945,
      100.0 * (p.open / NULLIF(pc.pc, 0) - 1) AS gap_pct,
      100.0 * (p.pre_close / NULLIF(p.open, 0) - 1) AS pre_drive_pct,
      100.0 * (p.pre_high - p.pre_low) / NULLIF(p.open, 0) AS or_range_pct,
      (p.pre_high - p.pre_low) / NULLIF(p.open, 0) / NULLIF(dr.avg_rel_range, 0) AS rv_ratio,
      p.pre_vol / NULLIF(dr.avg_day_vol, 0) AS vol_ratio,
      100.0 * (p.pre_maxhigh - p.pre_close) / NULLIF(p.open, 0) AS off_high_pct,
      100.0 * (p.pre_close - p.pre_minlow) / NULLIF(p.open, 0) AS off_low_pct
    FROM pre p
    JOIN prev_close pc USING (symbol, day)
    JOIN daily_ranked dr USING (symbol, day)
    JOIN post o USING (symbol, day)
    JOIN prev_vwap vw USING (symbol, day)
    WHERE p.pre_close > 0 AND p.open > 0 AND pc.pc > 0 AND p.pre_bars >= 20
    """
    df = _query(sql, [LAKE_GLOB])
    return df


def daily_ic(frame: pd.DataFrame, feat: str) -> pd.Series:
    """Per-day rank-IC (Pearson on ranks — no scipy needed) vs forward move."""
    def _rank_ic(g: pd.DataFrame) -> float:
        x = g[feat].rank()
        y = g["fwd_pct"].rank()
        if len(g) < 10 or x.std() == 0 or y.std() == 0:
            return np.nan
        return float(x.corr(y))

    return (
        frame.dropna(subset=[feat, "fwd_pct"]).groupby("day").apply(_rank_ic)
    )


def main() -> None:
    n_days = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    days = lake_days()
    days = days[-n_days:] if n_days else days
    print(f"lake sessions: {len(days)}  ({days[0]} → {days[-1]})")

    df = build_dataset(days)
    print(f"rows: {len(df):,}  symbols: {df.symbol.nunique():,}")
    df = df[df.fwd_pct.abs() < 60]  # data sanity
    print(f"forward move: mean {df.fwd_pct.mean():+.3f}%  median {df.fwd_pct.median():+.3f}%")
    print(f"days with ≥2% forward move: {(df.fwd_pct >= 2).mean() * 100:.1f}% of symbol-days")

    feats = [
        "gap_pct", "pre_drive_pct", "or_range_pct", "rv_ratio", "vol_ratio",
        "off_high_pct", "off_low_pct",
    ]
    print("\n=== daily rank-IC vs forward 09:45→15:15 move ===")
    print(f"{'feature':16s} {'meanIC':>8s} {'ICIR':>7s} {'IC>0':>6s}")
    results = {}
    for f in feats:
        ics = daily_ic(df, f).dropna()
        if len(ics) < 5:
            continue
        icir = ics.mean() / ics.std() if ics.std() else 0.0
        results[f] = (ics.mean(), icir, (ics > 0).mean())
        print(f"{f:16s} {ics.mean():+8.4f} {icir:+7.3f} {(ics > 0).mean():6.1%}")

    # Decile analysis for the most promising features
    print("\n=== decile mean forward move (Q1..Q10, by feature) ===")
    for f in feats:
        sub = df.dropna(subset=[f, "fwd_pct"])
        if len(sub) < 1000:
            continue
        q = sub.groupby(pd.qcut(sub[f], 10, labels=False, duplicates="drop"))["fwd_pct"].mean()
        spread = q.iloc[-1] - q.iloc[0]
        print(f"{f:16s} Q1={q.iloc[0]:+.2f}%  Q10={q.iloc[-1]:+.2f}%  Q10-Q1={spread:+.2f}%")

    # Top-k picker: each day pick top-k by feature, measure mean forward move + hit rate
    print("\n=== top-k daily picks: mean fwd move & %≥+2% (all days) ===")
    for f in feats + ["score_abs_minus", "score_drive_neg"]:
        if f.startswith("score"):
            if f == "score_abs_minus":
                key = -df.pre_drive_pct.abs().fillna(0)
            else:
                key = -df.pre_drive_pct.fillna(0)
            rank_col = key
            fname = f
        else:
            rank_col = df[f]
            fname = f
        sub = df.assign(_k=rank_col).dropna(subset=["_k", "fwd_pct"])
        picks = (
            sub.sort_values(["day", "_k"], ascending=[True, False])
            .groupby("day")
            .head(10)
        )
        if picks.empty:
            continue
        mean_fwd = picks.fwd_pct.mean()
        hit2 = (picks.fwd_pct >= 2).mean() * 100
        print(f"{fname:16s} n={len(picks):5d}  meanFwd={mean_fwd:+.2f}%  ≥+2%: {hit2:5.1f}%")

    # Random baseline
    rng = np.random.default_rng(7)
    sub = df.dropna(subset=["fwd_pct"])
    hits, means = [], []
    for _ in range(50):
        pick = sub.groupby("day", group_keys=False).apply(
            lambda g: g.sample(n=min(10, len(g)), random_state=int(rng.integers(1e9)))
        )
        means.append(pick.fwd_pct.mean())
        hits.append((pick.fwd_pct >= 2).mean() * 100)
    print(f"{'random-10':16s} meanFwd={np.mean(means):+.2f}%  ≥+2%: {np.mean(hits):5.1f}%")


if __name__ == "__main__":
    main()
