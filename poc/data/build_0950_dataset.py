"""Build a point-in-time-safe dataset for 09:50 -> 15:15 stock selection.

For each symbol and trading day D, every feature is computable at the 09:50
close. The target is only used after the decision point:

    target_return_pct = close_1515 / close_0950 - 1

The resulting rows are suitable for chronological ranking experiments. The
query deliberately calculates opening features from bars ``<= 09:50`` and
uses only prior days for rolling reference values.

Example::

    python poc/data/build_0950_dataset.py \
        --start 2026-01-01 --end 2026-09-08 \
        --output poc/data/returns_0950_1515.parquet
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO / "data" / "ohlcv"


def _month_paths(start: date, end: date) -> list[Path]:
    """Return only monthly partitions that can contain the requested range."""
    paths: list[Path] = []
    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    while cursor <= last:
        paths.extend(sorted(DATA_ROOT.glob(
            f"symbol=*/year={cursor.year:04d}/month={cursor.month:02d}/data.parquet"
        )))
        cursor = date(
            cursor.year + (cursor.month == 12),
            1 if cursor.month == 12 else cursor.month + 1,
            1,
        )
    if not paths:
        raise FileNotFoundError(f"no OHLCV partitions found under {DATA_ROOT}")
    return paths


def _sql_paths(paths: list[Path]) -> str:
    return "[" + ", ".join("'" + str(p).replace("'", "''") + "'" for p in paths) + "]"


def build_dataset(start: date, end: date, min_open: float) -> pd.DataFrame:
    # Include enough calendar lookback for the prior-session close and the
    # trailing 14-session comparable opening-volume average.
    lookback_start = start - timedelta(days=45)
    paths = _month_paths(lookback_start, end)
    parquet_paths = _sql_paths(paths)
    start_sql, end_sql = start.isoformat(), end.isoformat()

    sql = f"""
    WITH daily AS (
        SELECT
            symbol,
            CAST(timestamp AS DATE) AS trading_day,
            first(open ORDER BY timestamp) FILTER (
                WHERE CAST(timestamp AS TIME) >= TIME '09:15:00'
                  AND CAST(timestamp AS TIME) < TIME '15:30:00'
            ) AS day_open,
            first(close ORDER BY timestamp) FILTER (
                WHERE CAST(timestamp AS TIME) = TIME '09:50:00'
            ) AS close_0950,
            sum(volume) FILTER (
                WHERE CAST(timestamp AS TIME) >= TIME '09:15:00'
                  AND CAST(timestamp AS TIME) <= TIME '09:50:00'
            ) AS volume_to_0950,
            max(high) FILTER (
                WHERE CAST(timestamp AS TIME) <= TIME '09:50:00'
            ) AS high_to_0950,
            min(low) FILTER (
                WHERE CAST(timestamp AS TIME) <= TIME '09:50:00'
            ) AS low_to_0950,
            last(close ORDER BY timestamp) FILTER (
                WHERE CAST(timestamp AS TIME) >= TIME '09:50:00'
                  AND CAST(timestamp AS TIME) <= TIME '15:15:00'
            ) AS close_1515,
            last(close ORDER BY timestamp) FILTER (
                WHERE CAST(timestamp AS TIME) >= TIME '09:15:00'
                  AND CAST(timestamp AS TIME) < TIME '15:30:00'
            ) AS session_close
        FROM read_parquet({parquet_paths}, hive_partitioning=true)
        WHERE CAST(timestamp AS DATE) BETWEEN DATE '{lookback_start.isoformat()}'
                                           AND DATE '{end_sql}'
        GROUP BY symbol, CAST(timestamp AS DATE)
    ),
    with_history AS (
        SELECT
            *,
            lag(session_close) OVER (
                PARTITION BY symbol ORDER BY trading_day
            ) AS prev_session_close,
            avg(volume_to_0950) OVER (
                PARTITION BY symbol ORDER BY trading_day
                ROWS BETWEEN 14 PRECEDING AND 1 PRECEDING
            ) AS avg_prior_volume_to_0950
        FROM daily
    ),
    point_in_time AS (
        SELECT
            symbol,
            trading_day,
            day_open,
            close_0950,
            close_1515,
            (day_open / NULLIF(prev_session_close, 0) - 1) * 100
                AS open_gap_pct,
            (close_0950 / NULLIF(day_open, 0) - 1) * 100
                AS pre50_return_pct,
            volume_to_0950 / NULLIF(avg_prior_volume_to_0950, 0)
                AS relative_volume_to_0950,
            (high_to_0950 - low_to_0950) / NULLIF(day_open, 0) * 100
                AS opening_range_pct,
            (close_0950 - low_to_0950) / NULLIF(
                high_to_0950 - low_to_0950, 0
            ) AS opening_close_position,
            close_1515 / NULLIF(close_0950, 0) - 1
                AS target_return
        FROM with_history
        WHERE trading_day BETWEEN DATE '{start_sql}' AND DATE '{end_sql}'
          AND day_open >= {min_open}
          AND close_0950 IS NOT NULL
          AND close_1515 IS NOT NULL
          AND prev_session_close IS NOT NULL
          AND avg_prior_volume_to_0950 > 0
          AND high_to_0950 > low_to_0950
    ),
    ranked AS (
        SELECT
            *,
            row_number() OVER (
                PARTITION BY trading_day ORDER BY target_return DESC, symbol
            ) AS gainer_rank,
            count(*) OVER (PARTITION BY trading_day) AS universe_size
        FROM point_in_time
    )
    SELECT
        symbol,
        trading_day,
        day_open,
        close_0950,
        close_1515,
        open_gap_pct,
        pre50_return_pct,
        relative_volume_to_0950,
        opening_range_pct,
        opening_close_position,
        target_return * 100 AS target_return_pct,
        gainer_rank,
        universe_size,
        gainer_rank <= 5 AS is_top5,
        gainer_rank <= 10 AS is_top10,
        gainer_rank <= 20 AS is_top20
    FROM ranked
    ORDER BY trading_day, gainer_rank
    """

    con = duckdb.connect()
    try:
        frame = con.execute(sql).fetchdf()
    finally:
        con.close()
    if frame.empty:
        raise ValueError("no eligible symbol-days found for the requested range")
    frame["trading_day"] = pd.to_datetime(frame["trading_day"])
    return frame


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--min-open", type=float, default=50.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("poc/data/returns_0950_1515.parquet"),
    )
    args = parser.parse_args()
    if args.end < args.start:
        parser.error("--end must be on or after --start")

    frame = build_dataset(args.start, args.end, args.min_open)
    output = args.output if args.output.is_absolute() else REPO / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(output, index=False)

    print(f"saved {len(frame):,} rows to {output}")
    print(f"dates: {frame['trading_day'].min().date()} .. {frame['trading_day'].max().date()}")
    print(f"symbols/day: {frame.groupby('trading_day').size().median():.0f} median")
    print(f"target mean: {frame['target_return_pct'].mean():+.3f}%")
    print("top gainers:")
    for day, group in frame.groupby("trading_day", sort=True):
        top = group.nsmallest(5, "gainer_rank")
        values = ", ".join(
            f"{row.symbol} {row.target_return_pct:+.2f}%"
            for row in top.itertuples()
        )
        print(f"  {day.date()}: {values}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
