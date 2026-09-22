#!/usr/bin/env python3
"""Condition-by-condition breakdown for the pasted stacked expression.

This reconstructs the mechanically checkable parts of the pasted condition
against the local OHLCV parquet lake and prints a per-symbol pass/fail
report as of a real timestamp.

What is implemented here literally
-----------------------------------
c1  (prev_day_high + prev_day_low + prev_day_open + prev_day_close) / 4
    > (prev_day_open + prev_day_close) / 2
    Prior-session average of H/L/O/C vs average of O/C.
c2  today_running_volume > 250_000
c3  latest_close_as_of > prev_day_heikin_ashi_high
c4  latest_30m_close_as_of > 7_day_mean_daily_close
    Proxy for the pasted ``daily ^2264(...)`` term, which is a private study.
c5  latest_5m_close_as_of > rolling_30x30m_high  OR
    latest_5m_close_as_of < rolling_30x30m_low
    Proxy window: last 30 completed 30m buckets ending BEFORE today's session.
c6  30m_close > ADX(14) on 30m bars
    Proxy: latest 30m close > ADX(14) of the 30m series ending at as_of.
c7  daily_close > daily ATR(14)
c8  daily_avg_true_range_14 > 50

What is still a proxy / open decision
--------------------------------------
- ``daily ^2264('time'='daily close','lookback_days'='7','output'='val')``
  is a private study. This script uses
  ``latest_30m_close > mean(last 7 daily closes)`` as a transparent proxy.
- ``daily ^15728('amplitude'='2','output'='trend') = 1`` is a private study.
  This script uses ``latest_daily_close > SMA20`` as a transparent proxy and
  treats trend = 1 as ``close > SMA20``. Swap the proxy when the real formula
  is known.
- ADX(14) on 30m bars is implemented in SQL with a closed-form Wilder RSI
  style recurrence adapted to DX/ADX. If your runtime cannot support the
  expression cost or session Python is too old, drop it and use a
  Python-computed ADX joined back instead.

Session / intraday semantics used by this script
------------------------------------------------
- Session window for resampling: 09:15:00 .. 15:30:00.
- 30m and 5m buckets are built from session bars only, labeled by bucket
  start using NSE-style minute buckets anchored at 09:15.
- ``as_of`` limits every intraday read so historical replays see only bars
  up to that instant.
- ``[=1]`` in the pasted expression is interpreted as the prior completed
  30m bucket when evaluating c5 against the latest 5m close.

Usage
-----
python condition_breakdown.py
python condition_breakdown.py --as-of "2026-09-10 11:12:00"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd
import numpy as np

REPO = Path(__file__).resolve().parents[3]
GLOB = str(REPO / "data" / "ohlcv" / "**" / "data.parquet")

SESSION_OPEN = "09:15:00"
SESSION_CLOSE = "15:30:00"


def latest_timestamp(con) -> str:
    return str(con.execute(
        f"SELECT max(timestamp) FROM read_parquet('{GLOB}', hive_partitioning=true)"
    ).fetchone()[0])


def _bucket_sql(minutes: int) -> str:
    return (
        f"CAST(timestamp AS DATE) + (FLOOR(datediff('minute', "
        f"TIME '{SESSION_OPEN}', CAST(timestamp AS TIME)) / {minutes}) "
        f"* INTERVAL '{minutes} minutes')"
    )


def _daily_frame(con, as_of: str):
    """One row per symbol per completed session up to as_of."""
    as_of_ts = str(as_of)
    return con.execute(f"""
        SELECT
            symbol,
            CAST(timestamp AS DATE) AS d,
            first(open  ORDER BY timestamp) AS o,
            max(high)                AS h,
            min(low)                 AS l,
            last(close ORDER BY timestamp) AS c,
            sum(volume)              AS v,
            count(*)                 AS n_bars
        FROM read_parquet('{GLOB}', hive_partitioning=true)
        WHERE CAST(timestamp AS TIME) >= TIME '{SESSION_OPEN}'
          AND CAST(timestamp AS TIME) <= TIME '{SESSION_CLOSE}'
          AND timestamp <= TIMESTAMP '{as_of_ts}'
        GROUP BY symbol, d
        ORDER BY symbol, d
    """).fetchdf()


def _prior_row(daily):
    """Most recent completed session before today per symbol."""
    today = str(daily["d"].max())
    prior = daily[daily["d"] < today]
    return (
        prior.sort_values("d")
        .groupby("symbol")
        .tail(1)
        .rename(columns={"o": "o_y", "h": "h_y", "l": "l_y", "c": "c_y", "v": "v_y"})
        .reset_index(drop=True)
    )


def _heikin_ashi_high(daily):
    """Prior-session Heikin-Ashi high per symbol.

    ha_close = (o + h + l + c) / 4
    ha_open  = (prev_ha_open + prev_ha_close) / 2, seeded with
              (first_o + first_c) / 2
    ha_high  = max(high, ha_open, ha_close)
    """
    out = []
    for _, g in daily.groupby("symbol", sort=False):
        o = g["o"].to_numpy(float)
        h = g["h"].to_numpy(float)
        l = g["l"].to_numpy(float)
        c = g["c"].to_numpy(float)
        ha_c = (o + h + l + c) / 4.0
        ha_o = [None] * len(g)
        ha_o[0] = (o[0] + c[0]) / 2.0
        for i in range(1, len(g)):
            ha_o[i] = (ha_o[i - 1] + ha_c[i - 1]) / 2.0
        ha_h = np.maximum.reduce([h, np.asarray(ha_o), ha_c])
        out.append(
            pd.DataFrame({
                "symbol": g["symbol"].to_numpy(),
                "d": g["d"].to_numpy(),
                "ha_high": ha_h,
                "ha_close": ha_c,
                "ha_open": np.asarray(ha_o),
            })
        )
    if not out:
        return pd.DataFrame()
    return pd.concat(out, ignore_index=True)


def _adx14_30m(m30):
    """Wilder ADX(14) on 30m bars, latest value per symbol.

    Smoothed from true range / +DM / -DM with RMA (ewm alpha=1/14).
    """
    n = 14
    rows = []
    for sym, g in m30.groupby("symbol", sort=False):
        h = g["high"].to_numpy(float)
        l = g["low"].to_numpy(float)
        c = g["close"].to_numpy(float)
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
        rows.append({"symbol": sym, "adx": rma(dx)[-1], "n30": len(g)})
    return pd.DataFrame(rows)


def _struct_30x30(high30, low30, as_of_day):
    """Rolling max high / min low over the last 30 30m buckets ending before
    today's session. 'rolling 30 30m bars' = last 30 rows per symbol ordered
    by bucket, stopping before the current day's first bucket."""
    prior = high30[high30["bucket"] < pd.Timestamp(as_of_day)].copy()
    prior_low = low30[low30["bucket"] < pd.Timestamp(as_of_day)].copy()
    hi = (
        prior.sort_values("bucket")
        .groupby("symbol")
        .tail(30)
        .groupby("symbol")["high"]
        .max()
        .rename("hi30")
        .reset_index()
    )
    lo = (
        prior_low.sort_values("bucket")
        .groupby("symbol")
        .tail(30)
        .groupby("symbol")["low"]
        .min()
        .rename("lo30")
        .reset_index()
    )
    return hi.merge(lo, on="symbol", how="inner")


def _mean7_and_sma20(daily):
    """Per-symbol mean of last 7 daily closes and SMA20 as of yesterday."""
    today = str(daily["d"].max())
    prev = daily[daily["d"] < today]
    return (
        prev.groupby("symbol")
        .apply(
            lambda g: pd.Series({
                "mean7": g["c"].tail(7).mean(),
                "sma20": g["c"].tail(20).mean(),
            }),
            include_groups=False,
        )
        .reset_index()
    )


def _daily_atr14(daily):
    """Daily ATR(14) via Wilder smoothing, latest per symbol."""
    n = 14
    rows = []
    for sym, g in daily.groupby("symbol", sort=False):
        h = g["h"].to_numpy(float)
        l = g["l"].to_numpy(float)
        c = g["c"].to_numpy(float)
        if len(g) < n + 1:
            continue
        tr = np.maximum.reduce([
            h[1:] - l[1:],
            np.abs(h[1:] - c[:-1]),
            np.abs(l[1:] - c[:-1]),
        ])
        tr = np.concatenate([[h[0] - l[0]], tr])
        atr = pd.Series(tr).ewm(alpha=1.0 / n, adjust=False).mean().to_numpy()
        rows.append({"symbol": sym, "atr14": atr[-1], "close": c[-1]})
    return pd.DataFrame(rows)


def run(as_of: str, top: int = 40):
    con = duckdb.connect()
    try:
        as_of_ts = str(as_of)
        day = str(pd.Timestamp(as_of_ts).date())

        daily = _daily_frame(con, as_of_ts)
        if daily.empty:
            print("no session history up to as_of"); return

        prior = _prior_row(daily)
        ha = _heikin_ashi_high(daily)
        ha_y = ha[ha["d"] < pd.Timestamp(day)].groupby("symbol").tail(1)[["symbol", "ha_high"]]

        mean7 = _mean7_and_sma20(daily)

        atr = _daily_atr14(daily[daily["d"] < pd.Timestamp(day)])

        today_agg = con.execute(f"""
            SELECT
                symbol,
                sum(volume)                      AS vol_today,
                last(close ORDER BY timestamp)   AS last_close,
                max(CAST(timestamp AS TIME))     AS last_t
            FROM read_parquet('{GLOB}', hive_partitioning=true)
            WHERE CAST(timestamp AS DATE) = DATE '{day}'
              AND CAST(timestamp AS TIME) >= TIME '{SESSION_OPEN}'
              AND timestamp <= TIMESTAMP '{as_of_ts}'
            GROUP BY symbol
        """).fetchdf()

        m30 = con.execute(f"""
            SELECT
                symbol,
                {_bucket_sql(30)} AS bucket,
                first(open ORDER BY timestamp) AS open,
                max(high) AS high,
                min(low) AS low,
                last(close ORDER BY timestamp) AS close,
                sum(volume) AS vol
            FROM read_parquet('{GLOB}', hive_partitioning=true)
            WHERE timestamp <= TIMESTAMP '{as_of_ts}'
              AND CAST(timestamp AS DATE) >= DATE '{pd.Timestamp(day) - pd.Timedelta(days=14)}'
              AND CAST(timestamp AS TIME) >= TIME '{SESSION_OPEN}'
              AND CAST(timestamp AS TIME) < TIME '{SESSION_CLOSE}'
            GROUP BY symbol, bucket
            ORDER BY symbol, bucket
        """).fetchdf()

        m5 = con.execute(f"""
            SELECT
                symbol,
                {_bucket_sql(5)} AS bucket,
                last(close ORDER BY timestamp) AS close
            FROM read_parquet('{GLOB}', hive_partitioning=true)
            WHERE CAST(timestamp AS DATE) = DATE '{day}'
              AND CAST(timestamp AS TIME) >= TIME '{SESSION_OPEN}'
              AND CAST(timestamp AS TIME) < TIME '{SESSION_CLOSE}'
              AND timestamp <= TIMESTAMP '{as_of_ts}'
            GROUP BY symbol, bucket
            ORDER BY symbol, bucket
        """).fetchdf()

        # c5 window: last 30 completed 30m buckets ending before today.
        struct = _struct_30x30(m30[["symbol", "bucket", "high"]],
                                m30[["symbol", "bucket", "low"]], day)
        last_m5 = m5.groupby("symbol").tail(1)[["symbol", "close"]].rename(columns={"close": "close5"})
        last_m30 = m30.groupby("symbol").tail(1)[["symbol", "close", "vol"]].rename(
            columns={"close": "m30_close", "vol": "m30_vol"})
        adx_df = _adx14_30m(m30)

        f = (
            today_agg
            .merge(prior[["symbol", "o_y", "h_y", "l_y", "c_y"]], on="symbol", how="left")
            .merge(ha_y, on="symbol", how="left")
            .merge(mean7, on="symbol", how="left")
            .merge(atr, on="symbol", how="left")
            .merge(struct, on="symbol", how="left")
            .merge(last_m5, on="symbol", how="left")
            .merge(last_m30, on="symbol", how="left")
            .merge(adx_df.rename(columns={"close": "m30_close_adx"}).drop(columns=["n30"], errors="ignore"),
                   on="symbol", how="left")
        )

        f = f.dropna(subset=["h_y", "ha_high", "hi30"])
        if "v_y" in f.columns:
            f = f[f["v_y"] > 0.0]

        # c1
        f["c1"] = (f["h_y"] + f["l_y"] + f["o_y"] + f["c_y"]) / 4.0 > (f["o_y"] + f["c_y"]) / 2.0
        # c2
        f["c2"] = f["vol_today"] > 250_000
        # c3
        f["c3"] = f["last_close"] > f["ha_high"]
        # c4 proxy
        f["c4"] = f["m30_close"] > f["mean7"]
        # c5
        f["c5_breakout"] = f["close5"] > f["hi30"]
        f["c5_breakdown"] = f["close5"] < f["lo30"]
        f["c5"] = f["c5_breakout"] | f["c5_breakdown"]
        # c6 proxy: 30m close > ADX(14) on 30m
        f["c6"] = f["m30_close"] > f["adx"]
        # c7 proxy: trend=1 => close > SMA20
        f["c7"] = f["last_close"] > f["sma20"]
        # c8
        f["c8"] = f["atr14"] > 50.0
        # extra proxy condition for the pasted ^15728 trend=1 line
        f["c_trend_proxy"] = f["c7"]

        cond_cols = ["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"]
        f["n_pass"] = f[cond_cols].sum(axis=1)

        print(f"as-of: {as_of}  (day {day})\n")
        print("condition pass rates (of %d symbols with sufficient history):" % len(f))
        for col in cond_cols + ["c_trend_proxy"]:
            print(f"  {col:14}: {f[col].mean():5.1%}")
        print()

        hits = f[f[cond_cols].all(axis=1)].copy()
        hits["dir"] = np.where(hits["c5_breakout"], "breakout", "breakdown")
        hits = hits.sort_values(["n_pass", "vol_today"], ascending=[False, False])

        print(f"ALL 8 conditions matched: {len(hits)} symbols")
        if len(hits):
            show = hits.head(top)
            print(f"{'symbol':<14}{'last':>9}{'vol_today':>11}{'n_pass':>7}"
                  f"{'dir':>10}{'ha_high':>10}{'hi30':>9}{'lo30':>9}{'adx':>7}{'atr':>8}")
            for r in show.itertuples():
                print(
                    f"{r.symbol:<14}"
                    f"{r.last_close:>9.1f}"
                    f"{int(r.vol_today):>11,}"
                    f"{int(r.n_pass):>7}"
                    f"{r.dir:>10}"
                    f"{r.ha_high:>10.1f}"
                    f"{r.hi30:>9.1f}"
                    f"{r.lo30:>9.1f}"
                    f"{r.adx:>7.1f}"
                    f"{r.atr14:>8.1f}"
                )
            if len(hits) > len(show):
                print(f"  ... and {len(hits) - len(show)} more")

        print("\ntop 12 by n_pass then volume:")
        top_rows = f.sort_values(["n_pass", "vol_today"], ascending=[False, False]).head(12)
        print(f"{'symbol':<14}{'n_pass':>7}  failed")
        for r in top_rows.itertuples():
            failed = [c for c in cond_cols if not getattr(r, c)]
            print(f"{r.symbol:<14}{int(r.n_pass):>6}/8  {','.join(failed)}")

        print("\nnotes:")
        print("  c4 uses latest_30m_close > mean(last 7 daily closes) as a proxy")
        print("      for the private study daily ^2264(...).")
        print("  c7 uses latest_daily_close > SMA20 as a proxy for")
        print("      daily ^15728('amplitude'='2','output'='trend') = 1.")
        print("  c5 uses last 30 completed 30m buckets before today as the")
        print("      rolling high/low window, compared against the latest 5m close.")

    finally:
        con.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--as-of", default=None, help="'YYYY-MM-DD HH:MM' (default: latest bar)")
    ap.add_argument("--top", type=int, default=40)
    args = ap.parse_args(argv)

    con = duckdb.connect()
    as_of = args.as_of or latest_timestamp(con)
    con.close()
    run(as_of, args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
