"""Opening-window pattern study — can 09:15–09:45 data select the day's top
gainers (measured 09:45→15:15)?

Uses every trading day in the lake. Decision point is 09:45: all features use
only bars up to 09:45 plus the prior session's close. Classic patterns tested:

- Gap-and-Go      : overnight gap-up + first-30-min volume surge continues.
- Opening Drive   : strong 09:15→09:45 move + volume continues.
- ORB-close       : first-30-min bar closes near its high (range strength).
- ORB mechanical  : BUY break of first-30-min HIGH after 09:45, STOP under the
                    range LOW, flatten 15:15 — simulated fully in SQL.

Run: .venv/bin/python services/duckdb-analytics/research/opening_drive_study.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from duck_analytics.catalog import DuckDBCatalog
from duck_analytics.config import AnalyticsConfig
from duck_analytics.query import QueryService

FEATURE_SQL = """
WITH daily AS (
    SELECT symbol,
        ts::DATE AS d,
        first(open ORDER BY timestamp) AS o,
        last(close ORDER BY timestamp) AS c,
        max(CASE WHEN timestamp::TIME <= TIME '09:45' THEN high END) AS hi45,
        min(CASE WHEN timestamp::TIME <= TIME '09:45' THEN low END) AS lo45,
        last(close ORDER BY timestamp) FILTER (
            WHERE timestamp::TIME <= TIME '09:45') AS px45,
        sum(CASE WHEN timestamp::TIME < TIME '09:45'
                 THEN volume ELSE 0 END) AS v30,
        first(open ORDER BY timestamp) FILTER (
            WHERE timestamp::TIME >= TIME '09:45') AS o_win,
        last(close ORDER BY timestamp) FILTER (
            WHERE timestamp::TIME <= TIME '15:15') AS c_win
    FROM ohlcv
    WHERE year(ts::DATE) = 2026 AND month(ts::DATE) BETWEEN 5 AND 8
    GROUP BY symbol, d
),
seq AS (
    SELECT *,
        lag(c) OVER w_day AS prev_close,
        avg(v30) OVER (PARTITION BY symbol ORDER BY d
            ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING) AS base_v30
    FROM daily
    WINDOW w_day AS (PARTITION BY symbol ORDER BY d)
)
SELECT symbol,
       d,
       round((o / prev_close - 1) * 100, 3)                    AS gap_pct,
       round((px45 / o - 1) * 100, 3)                          AS pre30_ret,
       round(v30 / NULLIF(base_v30, 0), 2)                     AS v30x,
       round((px45 - lo45) / NULLIF(hi45 - lo45, 0), 3)        AS close_pos45,
       round((c_win / o_win - 1) * 100, 3)                     AS gain_target,
       row_number() OVER (PARTITION BY d ORDER BY
           (c_win / o_win - 1) DESC)                           AS rk_gainer
FROM seq
WHERE prev_close IS NOT NULL
  AND px45 IS NOT NULL AND o_win IS NOT NULL AND c_win IS NOT NULL
  AND o > 0 AND o_win > 0 AND hi45 > lo45 AND base_v30 > 0
"""

RULES: dict[str, str] = {
    # Gap-and-Go: overnight gap-up + volume surge (Cameron-style gapper).
    "R1_gap_and_go": "gap_pct >= 1.0 and v30x >= 2.0",
    # Opening Drive: strong first 30 minutes + above-normal volume.
    "R2_open_drive": "pre30_ret >= 0.7 and v30x >= 1.5",
    # Combined loose: either gap or drive, volume confirmation.
    "R3_combined": "(gap_pct >= 0.5 or pre30_ret >= 0.5) and v30x >= 2.0",
    # Pure volume anomaly, direction-free.
    "R4_vol_spike": "v30x >= 3.0",
    # ORB-strength: 30-min bar closes near its high + volume.
    "R5_range_close": "close_pos45 >= 0.9 and v30x >= 1.5",
}


def orb_sql(months: tuple[str, ...]) -> str:
    """Mechanical ORB backtest, fully pushed down to DuckDB.

    Entry: first 5m bar after 09:45 whose high breaks hi45 (+0.05% buffer);
    fills at max(bar open, trigger). Stop: lo45, pessimistic same-bar ordering
    (a bar that both triggers and touches the range low books the stop).
    Exit: 15:15 close when never stopped. Returns one row per triggered trade.
    """
    month_pred = " OR ".join(
        f"(year(ts::DATE) = {m[:4]} AND month(ts::DATE) = {int(m[5:])})"
        for m in months
    )
    return f"""
WITH ranges AS (
    SELECT * FROM (
        SELECT symbol, ts::DATE AS d,
            max(CASE WHEN timestamp::TIME <= TIME '09:45'
                     THEN high END) AS hi45,
            min(CASE WHEN timestamp::TIME <= TIME '09:45'
                     THEN low END) AS lo45
        FROM ohlcv
        WHERE {month_pred}
        GROUP BY symbol, d
    ) WHERE hi45 IS NOT NULL AND lo45 IS NOT NULL AND hi45 > lo45
),
b5 AS (
    SELECT symbol, ts::DATE AS d,
        time_bucket(INTERVAL '5 minutes', ts) AS bucket,
        first(open ORDER BY timestamp) AS open,
        max(high) AS high,
        min(low) AS low,
        last(close ORDER BY timestamp) AS close
    FROM ohlcv
    WHERE {month_pred}
      AND CAST(timestamp AS TIME) >= TIME '09:45'
      AND CAST(timestamp AS TIME) <= TIME '15:15'
    GROUP BY symbol, d, bucket
),
trig AS (
    SELECT b.symbol, b.d,
        min(b.bucket) FILTER (
            WHERE b.high >= r.hi45 * 1.0005) AS t_trig
    FROM b5 b JOIN ranges r USING (symbol, d)
    GROUP BY b.symbol, b.d
),
entry AS (
    SELECT b.symbol, b.d, e.t_trig,
        CASE WHEN b.open >= r.hi45 * 1.0005
             THEN b.open ELSE r.hi45 * 1.0005 END AS entry_px
    FROM b5 b
    JOIN trig e USING (symbol, d)
    JOIN ranges r USING (symbol, d)
    WHERE b.bucket = e.t_trig
),
stop_chk AS (
    SELECT e.symbol, e.d, e.entry_px,
        min(b.low) FILTER (
            WHERE b.bucket >= e.t_trig AND b.low <= r.lo45) AS hit_stop
    FROM entry e
    JOIN b5 b USING (symbol, d)
    JOIN ranges r USING (symbol, d)
    GROUP BY e.symbol, e.d, e.entry_px
),
last_px AS (
    SELECT symbol, d, last(close ORDER BY bucket) AS c_eod
    FROM b5 GROUP BY symbol, d
),
eligible AS (
    SELECT symbol, d FROM ranges
)
SELECT s.symbol, s.d, s.entry_px,
       CASE WHEN s.hit_stop IS NOT NULL
            THEN r.lo45 ELSE l.c_eod END / s.entry_px * 100 - 100 AS ret
FROM stop_chk s
JOIN ranges r USING (symbol, d)
JOIN last_px l USING (symbol, d)
ORDER BY s.d, ret DESC
"""


def main() -> None:
    cfg = AnalyticsConfig(base_path=Path("data/ohlcv"))
    cat = DuckDBCatalog(cfg)
    svc = QueryService(cat, cfg)

    res = svc.execute(FEATURE_SQL, limit=50_000)
    df = pd.DataFrame(res.rows, columns=[
        "symbol", "d", "gap_pct", "pre30_ret", "v30x", "close_pos45",
        "gain_target", "rk_gainer",
    ])
    df["d"] = pd.to_datetime(df["d"]).dt.strftime("%Y-%m-%d")
    df["is_top10"] = df["rk_gainer"] <= 10
    df["is_top20"] = df["rk_gainer"] <= 20
    df["month"] = df["d"].str[:7]
    df["r1_pass"] = df.eval(RULES["R1_gap_and_go"], engine="python")

    days = df["d"].nunique()
    print(f"dataset: {len(df)} symbol-days, {days} trading days")
    n_per_day = df.groupby("d").size().median()
    print(f"baseline P(top20)={20 / n_per_day:.1%}, P(top10)={10 / n_per_day:.1%}; "
          f"unconditional median intraday gain {df['gain_target'].median():+.2f}%\n")

    train = df[df["month"] < "2026-08"]
    test = df[df["month"] == "2026-08"]

    def evaluate(frame: pd.DataFrame, tag: str) -> list[str]:
        lines = [f"— {tag}: {len(frame)} rows, {frame['d'].nunique()} days"]
        for name, cond in RULES.items():
            sig = frame.query(cond, engine="python")
            if sig.empty:
                lines.append(f"  {name:<16} no signals")
                continue
            per_day = len(sig) / sig["d"].nunique()
            lines.append(
                f"  {name:<16} sig/day={per_day:4.1f}  "
                f"P(top20)={sig['is_top20'].mean():5.1%}  "
                f"P(top10)={sig['is_top10'].mean():5.1%}  "
                f"recall(top10)={sig['is_top10'].sum() / max(frame['is_top10'].sum(), 1):5.1%}  "
                f"med={sig['gain_target'].median():+.2f}%  "
                f"mean={sig['gain_target'].mean():+.2f}%  "
                f"win={(sig['gain_target'] > 0).mean():.0%}"
            )
        return lines

    print("\n".join(evaluate(train, "TRAIN May-Jul")))
    print()
    print("\n".join(evaluate(test, "TEST Aug")))
    print()

    print("feature lift (train): P(top10) by tercile")
    for feat_name in ["gap_pct", "pre30_ret", "v30x", "close_pos45"]:
        try:
            q = pd.qcut(train[feat_name], 3, duplicates="drop")
        except ValueError:
            continue
        tab = train.groupby(q, observed=True)["is_top10"].mean()
        cells = "  ".join(f"{str(i)}:{v:.1%}" for i, v in tab.items())
        print(f"  {feat_name:<12} {cells}")

    # ------------------------------------------------------------- ORB sim
    print("\nORB mechanical (entry=break hi45+0.05%, stop=lo45, exit 15:15):")
    for tag, months, frame in [
        ("TRAIN May-Jul", ("2026-05", "2026-06", "2026-07"), train),
        ("TEST Aug", ("2026-08",), test),
    ]:
        trades_res = svc.execute(orb_sql(months), limit=50_000)
        tr = pd.DataFrame(trades_res.rows, columns=["symbol", "d", "entry_px", "ret"])
        tr["d"] = pd.to_datetime(tr["d"]).dt.strftime("%Y-%m-%d")

        def report(sub: pd.DataFrame, label: str) -> None:
            if sub.empty:
                print(f"  {tag} {label}: no trades")
                return
            per_day = len(sub) / sub["d"].nunique()
            win = (sub["ret"] > 0).mean()
            losers = sub.loc[sub["ret"] <= 0, "ret"]
            winners = sub.loc[sub["ret"] > 0, "ret"]
            m = sub.merge(frame[["symbol", "d", "rk_gainer"]],
                          on=["symbol", "d"], how="left")
            p10 = (m["rk_gainer"] <= 10).mean()
            print(f"  {tag} {label:<14} trades/day={per_day:5.1f}  "
                  f"win={win:.0%}  med={sub['ret'].median():+.2f}%  "
                  f"mean={sub['ret'].mean():+.2f}%  "
                  f"avg_win={winners.mean() if len(winners) else float('nan'):+.2f}%  "
                  f"avg_loss={losers.mean() if len(losers) else float('nan'):+.2f}%  "
                  f"P(top10)={p10:.1%}")

        report(tr, "all-breakouts")
        flagged = tr.merge(
            frame[["symbol", "d", "r1_pass"]], on=["symbol", "d"], how="inner"
        )
        report(flagged[flagged["r1_pass"]], "ORB∩gap+vol")
    cat.close()


if __name__ == "__main__":
    main()
