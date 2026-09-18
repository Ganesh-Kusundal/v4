"""Walk-forward baseline ranker for the 09:50 -> 15:15 target.

This is intentionally a small, dependency-light baseline. It fits a ridge
regression on point-in-time opening features, ranks the eligible stocks within
each test day, and reports top-k selection metrics. Feature normalization and
model fitting use training days only.

Example::

    python poc/rank_0950.py \
        --data poc/data/returns_0950_1515.parquet \
        --train-days 40 --test-days 20 --top-k 5

This is not a trading recommendation. Results exclude costs and are useful
only as a baseline before adding TimesFM forecasts.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
FEATURES = [
    "open_gap_pct",
    "pre50_return_pct",
    "relative_volume_to_0950",
    "opening_range_pct",
    "opening_close_position",
]


def _fit_ridge(train: pd.DataFrame, alpha: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Fit standardized ridge regression and return coefficients plus scaling."""
    x = train[FEATURES].to_numpy(dtype=np.float64)
    y = train["target_return_pct"].to_numpy(dtype=np.float64)
    mean = np.nanmean(x, axis=0)
    std = np.nanstd(x, axis=0)
    std[std < 1e-12] = 1.0
    x = np.nan_to_num((x - mean) / std, nan=0.0, posinf=0.0, neginf=0.0)
    # Centering y makes the intercept unnecessary for within-day ranking.
    y = y - y.mean()
    gram = x.T @ x + alpha * np.eye(x.shape[1])
    coef = np.linalg.solve(gram, x.T @ y)
    return coef, mean, std


def _predict(frame: pd.DataFrame, coef: np.ndarray, mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    x = frame[FEATURES].to_numpy(dtype=np.float64)
    x = np.nan_to_num((x - mean) / std, nan=0.0, posinf=0.0, neginf=0.0)
    return x @ coef


def _ndcg(actual: np.ndarray, predicted_order: np.ndarray, k: int) -> float:
    """NDCG using returns converted to non-negative gains within the day."""
    k = min(k, len(actual))
    gains = actual - np.min(actual)
    ideal = np.sort(gains)[::-1][:k]
    chosen = gains[predicted_order[:k]]
    discounts = 1.0 / np.log2(np.arange(k) + 2)
    denom = float(np.sum(ideal * discounts))
    return float(np.sum(chosen * discounts) / denom) if denom > 0 else 0.0


def evaluate_day(frame: pd.DataFrame, scores: np.ndarray, top_k: int) -> dict[str, float]:
    actual = frame["target_return_pct"].to_numpy(dtype=float)
    k = min(top_k, len(frame))
    pred_order = np.argsort(-scores, kind="stable")
    actual_order = np.argsort(-actual, kind="stable")
    selected = pred_order[:k]
    actual_top = set(actual_order[:k])
    return {
        "n": float(len(frame)),
        "precision": len(set(selected) & actual_top) / k,
        "selected_return": float(np.mean(actual[selected])),
        "buy_all_return": float(np.mean(actual)),
        "top_return": float(np.mean(actual[actual_order[:k]])),
        "ndcg": _ndcg(actual, pred_order, k),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("poc/data/returns_0950_1515.parquet"))
    parser.add_argument("--train-days", type=int, default=40)
    parser.add_argument("--test-days", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--alpha", type=float, default=10.0)
    args = parser.parse_args()

    path = args.data if args.data.is_absolute() else REPO / args.data
    df = pd.read_parquet(path)
    df["trading_day"] = pd.to_datetime(df["trading_day"])
    df = df.sort_values(["trading_day", "symbol"]).reset_index(drop=True)
    days = sorted(df["trading_day"].unique())
    if args.train_days < 1 or args.test_days < 1:
        parser.error("--train-days and --test-days must be positive")
    if len(days) < args.train_days + args.test_days:
        parser.error(
            f"dataset has {len(days)} days but needs at least "
            f"{args.train_days + args.test_days}"
        )

    test_days = days[-args.test_days:]
    train_days = days[:-args.test_days]
    if len(train_days) < args.train_days:
        parser.error("not enough pre-test days for --train-days")
    train_days = train_days[-args.train_days:]
    train = df[df["trading_day"].isin(train_days)].dropna(subset=["target_return_pct"])
    coef, mean, std = _fit_ridge(train, args.alpha)

    rows: list[dict[str, float | str]] = []
    for day in test_days:
        group = df[df["trading_day"] == day].dropna(subset=FEATURES + ["target_return_pct"])
        if len(group) < args.top_k:
            continue
        scores = _predict(group, coef, mean, std)
        metrics = evaluate_day(group, scores, args.top_k)
        rows.append({"trading_day": str(pd.Timestamp(day).date()), **metrics})

    result = pd.DataFrame(rows)
    if result.empty:
        raise RuntimeError("no eligible test days")
    print(f"train: {train_days[0].date()} .. {train_days[-1].date()} ({len(train_days)} days)")
    print(f"test:  {test_days[0].date()} .. {test_days[-1].date()} ({len(test_days)} days)")
    print(f"features: {', '.join(FEATURES)}")
    print(f"ridge alpha: {args.alpha:g}; top-k: {args.top_k}")
    print()
    print(f"precision@{args.top_k}:      {result['precision'].mean():.1%}")
    print(f"NDCG@{args.top_k}:            {result['ndcg'].mean():.3f}")
    print(f"selected return:             {result['selected_return'].mean():+.3f}%")
    print(f"buy-all return:              {result['buy_all_return'].mean():+.3f}%")
    print(f"perfect top-{args.top_k}:           {result['top_return'].mean():+.3f}%")
    print(f"selection edge vs buy-all:   "
          f"{result['selected_return'].mean() - result['buy_all_return'].mean():+.3f}%")
    print()
    print("coefficient direction (standardized features):")
    for name, value in zip(FEATURES, coef, strict=True):
        print(f"  {name:<28} {value:+.4f}")
    print()
    print("per-day summary:")
    for row in result.itertuples(index=False):
        print(
            f"  {row.trading_day}  n={row.n:.0f}  "
            f"precision={row.precision:.0%}  selected={row.selected_return:+.2f}%  "
            f"all={row.buy_all_return:+.2f}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
