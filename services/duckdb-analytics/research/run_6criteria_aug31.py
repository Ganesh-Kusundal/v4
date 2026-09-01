"""Run 6-criteria opening screener for a single day (parquet / DuckDB).

Criteria @ 09:45:
  1. RVOL 7d >= 2x (first-30m vol vs prior 7d avg)  [shown, not filtered]
  2. Move 2-4% (open -> 09:45 close)
  3. Fade <= 0.75% (morning high -> 09:45 close)
  4. Close > MA5, 100 < MA5 < 10001 (5m bars)
  5. Vol > 2x MA20 (5m bars)
  6. Breakout > 13-bar high (5m bars, prior 13 highs)

Convention: "as of 09:45:00" uses only data through 09:44:59. Minute bars are
open-time stamped (bar 09:45 covers 09:45:00-09:45:59, so its close is only
known at 09:45:59 and must NOT be used for a 09:45 signal). The signal 5m bar
is the complete 09:40 bucket (minutes 09:40-09:44). This keeps every 5m
bucket complete (5 minute bars each) so vol_5m is comparable to vol_ma20;
previously the signal bucket held a single minute bar, deflating vol_5m ~5x
against 5-minute MA buckets.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

DAY = "2026-08-31"
AS_OF = f"{DAY} 09:45:00"
REPO = Path(__file__).resolve().parents[3]  # .../v4
GLOB = str(REPO / "data/ohlcv/**/data.parquet")


def main() -> None:
    con = duckdb.connect(":memory:")
    con.execute(
        f"CREATE VIEW ohlcv AS SELECT *, CAST(timestamp AS TIMESTAMP) AS ts "
        f"FROM read_parquet('{GLOB}', hive_partitioning=true) "
        "WHERE CAST(timestamp AS TIME) >= TIME '09:15:00' "
        "AND CAST(timestamp AS TIME) <= TIME '15:30:00'"
    )

    sql = f"""
WITH daily AS (
    SELECT symbol, ts::DATE AS d,
        first(open ORDER BY timestamp) AS o,
        sum(CASE WHEN CAST(timestamp AS TIME) < TIME '09:45' THEN volume ELSE 0 END) AS v30,
        last(close ORDER BY timestamp) FILTER (
            WHERE CAST(timestamp AS TIME) < TIME '09:45') AS px45,
        max(high) FILTER (WHERE CAST(timestamp AS TIME) < TIME '09:45') AS hi45,
        min(low) FILTER (WHERE CAST(timestamp AS TIME) < TIME '09:45') AS lo45
    FROM ohlcv
    GROUP BY symbol, d
),
rvol AS (
    SELECT symbol, d, o, px45, hi45, lo45, v30,
        round(v30 / NULLIF(avg(v30) OVER (
            PARTITION BY symbol ORDER BY d
            ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING), 0), 2) AS rvol_7d,
        round((px45 / NULLIF(o, 0) - 1) * 100, 2) AS move_0945_pct,
        round((hi45 - px45) / NULLIF(hi45, 0) * 100, 2) AS fade_pct
    FROM daily
),
-- 5m bars through 09:45 on signal day; MA/lookbacks use prior bars (multi-day).
b5 AS (
    SELECT symbol, ts::DATE AS d,
        time_bucket(INTERVAL '5 minutes', ts) AS bucket,
        first(open ORDER BY timestamp) AS open,
        max(high) AS high,
        min(low) AS low,
        last(close ORDER BY timestamp) AS close,
        sum(volume) AS vol
    FROM ohlcv
    WHERE timestamp < TIMESTAMP '{AS_OF}'
      AND CAST(timestamp AS TIME) >= TIME '09:15'
      AND CAST(timestamp AS TIME) < TIME '09:45'
    GROUP BY symbol, d, bucket
),
b5_seq AS (
    SELECT symbol, d, bucket, close, vol, high,
        row_number() OVER (PARTITION BY symbol ORDER BY d, bucket) AS rn
    FROM b5
),
b5_ind AS (
    SELECT symbol, d, bucket, close, vol, high,
        avg(close) OVER (PARTITION BY symbol ORDER BY rn
            ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS ma5,
        avg(vol) OVER (PARTITION BY symbol ORDER BY rn
            ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS vol_ma20,
        max(high) OVER (PARTITION BY symbol ORDER BY rn
            ROWS BETWEEN 13 PRECEDING AND 1 PRECEDING) AS hi13_prev
    FROM b5_seq
),
b5_at_0945 AS (
    SELECT * FROM b5_ind
    WHERE d = DATE '{DAY}'
      AND bucket = time_bucket(INTERVAL '5 minutes', TIMESTAMP '{AS_OF}' - INTERVAL '5 minutes')
),
joined AS (
    SELECT r.symbol, r.d, r.o, r.px45, r.hi45, r.rvol_7d,
           r.move_0945_pct, r.fade_pct,
           t.close AS close_5m, t.ma5, t.vol AS vol_5m, t.vol_ma20, t.hi13_prev
    FROM rvol r
    JOIN b5_at_0945 t USING (symbol, d)
    WHERE r.d = DATE '{DAY}'
),
flags AS (
    SELECT *,
        move_0945_pct >= 2.0 AND move_0945_pct <= 4.0 AS ok_move,
        fade_pct <= 0.75 AS ok_fade,
        close_5m > ma5 AND ma5 > 100 AND ma5 < 10001 AS ok_ma5,
        vol_5m > 2.0 * vol_ma20 AS ok_vol,
        close_5m > hi13_prev AS ok_breakout
    FROM joined
)
SELECT symbol, o, px45, hi45, rvol_7d, move_0945_pct, fade_pct,
       round(ma5, 2) AS ma5, close_5m, vol_5m, vol_ma20, hi13_prev,
       ok_move, ok_fade, ok_ma5, ok_vol, ok_breakout
FROM flags
WHERE ok_move AND ok_fade AND ok_ma5 AND ok_vol AND ok_breakout
ORDER BY move_0945_pct DESC
"""
    rows = con.execute(sql).fetchall()

    diag_sql = f"""
WITH daily AS (
    SELECT symbol, ts::DATE AS d,
        first(open ORDER BY timestamp) AS o,
        sum(CASE WHEN CAST(timestamp AS TIME) < TIME '09:45' THEN volume ELSE 0 END) AS v30,
        last(close ORDER BY timestamp) FILTER (
            WHERE CAST(timestamp AS TIME) < TIME '09:45') AS px45,
        max(high) FILTER (WHERE CAST(timestamp AS TIME) < TIME '09:45') AS hi45
    FROM ohlcv GROUP BY symbol, d
),
rvol AS (
    SELECT symbol, d,
        round(v30 / NULLIF(avg(v30) OVER (
            PARTITION BY symbol ORDER BY d ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING), 0), 2) AS rvol_7d,
        round((px45 / NULLIF(o, 0) - 1) * 100, 2) AS move_0945_pct,
        round((hi45 - px45) / NULLIF(hi45, 0) * 100, 2) AS fade_pct
    FROM daily
),
b5 AS (
    SELECT symbol, ts::DATE AS d, time_bucket(INTERVAL '5 minutes', ts) AS bucket,
        max(high) AS high, last(close ORDER BY timestamp) AS close, sum(volume) AS vol
    FROM ohlcv
    WHERE timestamp < TIMESTAMP '{AS_OF}'
      AND CAST(timestamp AS TIME) BETWEEN TIME '09:15' AND TIME '09:44'
    GROUP BY symbol, d, bucket
),
b5_seq AS (
    SELECT symbol, d, bucket, close, vol, high,
        row_number() OVER (PARTITION BY symbol ORDER BY d, bucket) AS rn
    FROM b5
),
b5_ind AS (
    SELECT symbol, d, bucket, close, vol, high,
        avg(close) OVER (PARTITION BY symbol ORDER BY rn
            ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS ma5,
        avg(vol) OVER (PARTITION BY symbol ORDER BY rn
            ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS vol_ma20,
        max(high) OVER (PARTITION BY symbol ORDER BY rn
            ROWS BETWEEN 13 PRECEDING AND 1 PRECEDING) AS hi13_prev
    FROM b5_seq
),
b5_at AS (
    SELECT * FROM b5_ind
    WHERE d = DATE '{DAY}'
      AND bucket = time_bucket(INTERVAL '5 minutes', TIMESTAMP '{AS_OF}' - INTERVAL '5 minutes')
),
j AS (
    SELECT r.*, b.close AS close_5m, b.ma5, b.vol AS vol_5m, b.vol_ma20, b.hi13_prev
    FROM rvol r JOIN b5_at b USING (symbol, d)
    WHERE r.d = DATE '{DAY}'
)
SELECT
    sum(CASE WHEN rvol_7d >= 2 THEN 1 ELSE 0 END),
    sum(CASE WHEN move_0945_pct BETWEEN 2 AND 4 THEN 1 ELSE 0 END),
    sum(CASE WHEN fade_pct <= 0.75 THEN 1 ELSE 0 END),
    sum(CASE WHEN close_5m > ma5 AND ma5 > 100 AND ma5 < 10001 THEN 1 ELSE 0 END),
    sum(CASE WHEN vol_5m > 2 * vol_ma20 THEN 1 ELSE 0 END),
    sum(CASE WHEN close_5m > hi13_prev THEN 1 ELSE 0 END),
    sum(CASE WHEN move_0945_pct BETWEEN 2 AND 4 AND fade_pct <= 0.75
        AND close_5m > ma5 AND ma5 > 100 AND ma5 < 10001
        AND vol_5m > 2 * vol_ma20 AND close_5m > hi13_prev THEN 1 ELSE 0 END),
    count(*)
FROM j
"""
    diag = con.execute(diag_sql).fetchone()

    print(f"=== 6-CRITERIA SCREENER — {DAY} @ 09:45 ===")
    print("Move 2-4% | Fade<=0.75% | Close>MA5 & 100<MA5<10001 | Vol>2xMA20 | Break>13hi (+ RVOL 7d shown)")
    print()
    print(f"Universe (has signal 5m bar): {diag[7]}")
    print(f"  RVOL 7d >= 2x (info):  {diag[0]}")
    print(f"  Pass Move 2-4%:        {diag[1]}")
    print(f"  Pass Fade:             {diag[2]}")
    print(f"  Pass MA5 band:         {diag[3]}")
    print(f"  Pass Vol>2xMA20:       {diag[4]}")
    print(f"  Pass 13-bar break:     {diag[5]}")
    print(f"  Pass ALL 5 filters:    {diag[6]}")
    print()

    if rows:
        hdr = f"{'Symbol':<12} {'Open':>8} {'@09:45':>8} {'RVOL':>6} {'Move':>7} {'Fade':>6} {'MA5':>8} {'V/MA20':>7} {'Hi13':>8}"
        print(hdr)
        print("-" * len(hdr))
        for r in rows:
            sym, o, px45, hi45, rvol, move, fade, ma5, c5, v5, vma, hi13, *_ = r
            ratio = v5 / vma if vma else 0
            print(
                f"{sym:<12} {o:>8.2f} {px45:>8.2f} {rvol:>6.2f} {move:>6.2f}% {fade:>5.2f}% "
                f"{ma5:>8.2f} {ratio:>6.1f}x {hi13:>8.2f}"
            )
    else:
        print("No stocks passed all 6 criteria.")
        partial = con.execute(
            f"""
            WITH             daily AS (
                SELECT symbol, ts::DATE AS d, first(open ORDER BY timestamp) AS o,
                    last(close ORDER BY timestamp) FILTER (
                        WHERE CAST(timestamp AS TIME) < TIME '09:45') AS px45,
                    max(high) FILTER (WHERE CAST(timestamp AS TIME) < TIME '09:45') AS hi45,
                    sum(CASE WHEN CAST(timestamp AS TIME) < TIME '09:45'
                        THEN volume ELSE 0 END) AS v30
                FROM ohlcv GROUP BY symbol, d
            ),
            rvol_all AS (
                SELECT symbol, d, o, px45, hi45,
                    round(v30 / NULLIF(avg(v30) OVER (
                        PARTITION BY symbol ORDER BY d ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING), 0), 2) AS rvol_7d,
                    round((px45 / NULLIF(o, 0) - 1) * 100, 2) AS move_pct,
                    round((hi45 - px45) / NULLIF(hi45, 0) * 100, 2) AS fade_pct
                FROM daily
            ),
            rvol AS (
                SELECT * FROM rvol_all WHERE d = DATE '{DAY}'
            )
            SELECT symbol, o, px45, rvol_7d, move_pct, fade_pct FROM rvol
            WHERE move_pct BETWEEN 2 AND 4 ORDER BY move_pct DESC
            """
        ).fetchall()
        print("\nMove 2-4% @ 09:45 (criterion breakdown):")
        for p in partial:
            sym, o, px45, rvol7d, move, fade = p
            fails = []
            if rvol7d is not None and rvol7d < 2:
                fails.append(f"RVOL={rvol7d:.2f}")
            if fade > 0.75:
                fails.append(f"fade={fade:.2f}%")
            print(f"  {sym:<12} move={move:.2f}% rvol7d={rvol7d or 'n/a'} fade={fade:.2f}%  fail: {', '.join(fails) or '5m tech'}")


if __name__ == "__main__":
    main()
