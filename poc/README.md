# POC — 09:50 → 15:15 top-gainer selection

This POC studies whether stocks that are likely to become the day's top
09:50→15:15 gainers can be selected using only information available at the
09:50 close. It is an offline research workflow, not a trading recommendation.

## Current workflow

```bash
# Build labels and point-in-time-safe opening features.
python poc/data/build_0950_dataset.py \
  --start 2026-01-01 --end 2026-09-08 \
  --output poc/data/returns_0950_1515.parquet

# Run the first chronological ranking baseline.
python poc/rank_0950.py \
  --data poc/data/returns_0950_1515.parquet \
  --train-days 40 --test-days 20 --top-k 5
```

The builder uses monthly parquet partition pruning and produces one row per
eligible `(symbol, trading_day)` pair with:

- `open_gap_pct`: day open versus previous session close;
- `pre50_return_pct`: day open to the 09:50 close;
- `relative_volume_to_0950`: volume through 09:50 versus the prior 14-session average;
- `opening_range_pct`: high-low range through 09:50;
- `opening_close_position`: 09:50 close location in that opening range;
- `target_return_pct`: 09:50 close to 15:15 close, used only as the label;
- `gainer_rank`, `is_top5`, `is_top10`, and `is_top20`.

All feature bars are at or before 09:50. The target is read through 15:15
only for evaluation/training labels. The rank is calculated within each day.

## Baseline model

`rank_0950.py` is deliberately dependency-light. It fits standardized ridge
regression on historical days only, then ranks stocks cross-sectionally on
each test day. It reports precision@k, NDCG@k, selected portfolio return,
buy-all return, and a hindsight top-k upper bound.

The initial baseline is required before adding TimesFM. Its purpose is to
answer whether the opening features contain signal at all, independently of a
large pretrained model.

## TimesFM next step

TimesFM should first be tested as an additional **zero-shot ranking feature**:

1. construct each stock's 1-minute close context ending at 09:49;
2. forecast the remaining 09:50→15:15 path;
3. calculate the predicted 15:15 return;
4. add that score to the baseline ranker;
5. compare against the feature-only model using the same walk-forward days.

The forecast horizon must represent the actual remaining number of 1-minute
observations (about 326 with inclusive endpoints), not the previous 35-bar
configuration. Use `predict_batch()` for the universe and explicitly compare
median and upper-quantile scores.

Fine-tuning is deferred until the zero-shot feature and the supervised
baseline are valid over a substantially larger history. TimesFM's
`predict()` method is inference-only and cannot be used as a differentiable
fine-tuning loop.

## Evaluation rules

- Never use `close_1515`, full-day high/low, or full-day volume as a feature.
- Split by date, never randomly by row.
- Keep a final untouched test period.
- Compare against buy-all, prior momentum, opening gap/volume rules, and
  perfect hindsight top-k only as an upper bound.
- Add costs, slippage, liquidity limits, and confidence intervals before using
  any result operationally.

## Existing scripts

The older `exp_*.py`, `backtest_*.py`, and `train_finetune.py` files are
historical experiments. They use the earlier 09:45 target and should not be
used to claim performance for the new 09:50 target until migrated.
