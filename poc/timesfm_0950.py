"""Evaluate TimesFM as a zero-shot 09:50 -> 15:15 ranking feature.

The script uses regular-session closes through the 09:50 entry close as
context, forecasts the remaining session, and compares median and 0.9-quantile
predicted returns against the realized cross-sectional ranks in the prepared
dataset.

Example::

    python poc/timesfm_0950.py \
        --data poc/data/returns_0950_1515.parquet \
        --days 2 --symbols 20 \
        --output poc/data/timesfm_0950_predictions.parquet

The ``--symbols`` limit is useful for a smoke test. Leave it at zero to run
the complete eligible universe. This is inference only; TimesFM's public
``predict`` API is not a differentiable fine-tuning path.
"""

from __future__ import annotations

import argparse
import os
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from timesfm import TimesFM3Forecaster

REPO = Path(__file__).resolve().parents[1]
DATA_ROOT = REPO / "data" / "ohlcv"
MODEL = os.path.expanduser(
    "~/.cache/huggingface/hub/models--google--timesfm-3.0-pytorch/"
    "snapshots/43046b85ec22d584a13f8098c2ed39c889e129c2"
)
SESSION_OPEN = "09:15:00"
DECISION_TIME = "09:50:00"
SESSION_CLOSE = "15:30:00"
TARGET_CLOSE = "15:15:00"
CONTEXT_BARS = 14 * 375 + 36  # 14 full sessions plus 09:15..09:50 on D.
HORIZON_BARS = 325  # 09:51..15:15 after entering at the 09:50 close.


def _month_paths(start: date, end: date) -> list[Path]:
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


def _rank_metrics(frame: pd.DataFrame, score: str, top_k: int) -> dict[str, float]:
    rows: list[dict[str, float]] = []
    for _, day_frame in frame.groupby("trading_day", sort=True):
        day_frame = day_frame.dropna(subset=[score, "target_return_pct"])
        if len(day_frame) < top_k:
            continue
        pred = day_frame[score].to_numpy(float)
        actual = day_frame["target_return_pct"].to_numpy(float)
        pred_order = np.argsort(-pred, kind="stable")
        actual_order = np.argsort(-actual, kind="stable")
        selected = pred_order[:top_k]
        rows.append({
            "precision": len(set(selected) & set(actual_order[:top_k])) / top_k,
            "selected_return": float(actual[selected].mean()),
            "buy_all_return": float(actual.mean()),
            "ndcg": _ndcg(actual, pred_order, top_k),
        })
    if not rows:
        return {"days": 0.0}
    result = pd.DataFrame(rows)
    return {
        "days": float(len(result)),
        "precision": float(result["precision"].mean()),
        "selected_return": float(result["selected_return"].mean()),
        "buy_all_return": float(result["buy_all_return"].mean()),
        "ndcg": float(result["ndcg"].mean()),
    }


def _ndcg(actual: np.ndarray, order: np.ndarray, k: int) -> float:
    gains = actual - actual.min()
    discounts = 1.0 / np.log2(np.arange(k) + 2)
    ideal = np.sort(gains)[::-1][:k]
    chosen = gains[order[:k]]
    denominator = float((ideal * discounts).sum())
    return float((chosen * discounts).sum() / denominator) if denominator > 0 else 0.0


def _load_contexts(
    symbols: list[str], days: list[pd.Timestamp],
) -> dict[tuple[str, str], np.ndarray]:
    start = days[0].date() - timedelta(days=45)
    end = days[-1].date()
    paths = _month_paths(start, end)
    path_sql = _sql_paths(paths)
    symbols_sql = "[" + ", ".join(
        "'" + symbol.replace("'", "''") + "'" for symbol in symbols
    ) + "]"
    start_sql, end_sql = start.isoformat(), end.isoformat()
    sql = f"""
        SELECT symbol, timestamp, close
        FROM read_parquet({path_sql}, hive_partitioning=true)
        WHERE symbol IN (SELECT * FROM unnest({symbols_sql}))
          AND CAST(timestamp AS DATE) BETWEEN DATE '{start_sql}' AND DATE '{end_sql}'
          AND CAST(timestamp AS TIME) >= TIME '{SESSION_OPEN}'
          AND CAST(timestamp AS TIME) < TIME '{SESSION_CLOSE}'
        ORDER BY symbol, timestamp
    """
    con = duckdb.connect()
    try:
        bars = con.execute(sql).fetchdf()
    finally:
        con.close()
    bars["timestamp"] = pd.to_datetime(bars["timestamp"])
    wanted = {pd.Timestamp(day).date() for day in days}
    contexts: dict[tuple[str, str], np.ndarray] = {}
    for symbol, group in bars.groupby("symbol", sort=False):
        group = group.sort_values("timestamp")
        for day in wanted:
            cutoff = pd.Timestamp(day) + pd.Timedelta(hours=9, minutes=50)
            values = group.loc[group["timestamp"] <= cutoff, "close"].to_numpy(dtype=np.float32)
            if len(values) >= CONTEXT_BARS:
                contexts[(symbol, str(day))] = values[-CONTEXT_BARS:]
    return contexts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("poc/data/returns_0950_1515.parquet"))
    parser.add_argument("--model", type=str, default=MODEL)
    parser.add_argument("--days", type=int, default=20)
    parser.add_argument("--symbols", type=int, default=0, help="0 means all symbols")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if args.days < 1 or args.symbols < 0 or args.top_k < 1:
        parser.error("--days and --top-k must be positive; --symbols cannot be negative")

    data_path = args.data if args.data.is_absolute() else REPO / args.data
    frame = pd.read_parquet(data_path)
    frame["trading_day"] = pd.to_datetime(frame["trading_day"])
    days = sorted(frame["trading_day"].unique())[-args.days:]
    universe = sorted(frame.loc[frame["trading_day"].isin(days), "symbol"].unique())
    if args.symbols:
        universe = universe[:args.symbols]
    if len(universe) < args.top_k:
        parser.error(f"only {len(universe)} symbols available, need at least {args.top_k}")

    print(f"days: {days[0].date()} .. {days[-1].date()} ({len(days)})")
    print(f"symbols requested: {len(universe)}")
    print(f"context: {CONTEXT_BARS} bars ending at the {DECISION_TIME} entry close")
    print(f"horizon: {HORIZON_BARS} bars through {TARGET_CLOSE}")
    print("loading contexts...")
    contexts = _load_contexts(universe, [pd.Timestamp(day) for day in days])
    print(f"usable contexts: {len(contexts)}")

    print("loading TimesFM...")
    fm = TimesFM3Forecaster.from_pretrained(args.model)
    records: list[dict[str, object]] = []
    for day in days:
        day_key = str(pd.Timestamp(day).date())
        keys = [(symbol, day_key) for symbol in universe if (symbol, day_key) in contexts]
        if len(keys) < args.top_k:
            print(f"{day_key}: skipped; only {len(keys)} usable contexts")
            continue
        outputs = fm.predict_batch(
            contexts=[contexts[key] for key in keys],
            horizon=HORIZON_BARS,
            return_quantiles=True,
        )
        actual = frame[frame["trading_day"] == day].set_index("symbol")
        for key, output in zip(keys, outputs, strict=True):
            symbol = key[0]
            if symbol not in actual.index or output.forecast is None:
                continue
            context = contexts[key]
            median_close = float(np.asarray(output.forecast)[-1])
            quantiles = np.asarray(output.quantiles)
            p90_close = float(quantiles[-1, 8])
            entry = float(context[-1])
            row = actual.loc[symbol]
            records.append({
                "symbol": symbol,
                "trading_day": day,
                "timesfm_median_return_pct": (median_close / entry - 1) * 100,
                "timesfm_p90_return_pct": (p90_close / entry - 1) * 100,
                "target_return_pct": float(row["target_return_pct"]),
                "gainer_rank": int(row["gainer_rank"]),
            })
        print(f"{day_key}: predicted {len(keys)} symbols")

    result = pd.DataFrame(records)
    if result.empty:
        raise RuntimeError("no forecasts produced")
    print()
    for score in ["timesfm_median_return_pct", "timesfm_p90_return_pct"]:
        metrics = _rank_metrics(result, score, args.top_k)
        print(f"{score}:")
        print(f"  precision@{args.top_k}: {metrics['precision']:.1%}")
        print(f"  NDCG@{args.top_k}:       {metrics['ndcg']:.3f}")
        print(f"  selected return:        {metrics['selected_return']:+.3f}%")
        print(f"  buy-all return:         {metrics['buy_all_return']:+.3f}%")

    if args.output is not None:
        output = args.output if args.output.is_absolute() else REPO / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        result.to_parquet(output, index=False)
        print(f"saved predictions to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
