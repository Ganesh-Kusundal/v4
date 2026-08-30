"""Volume-price entry study — findings recorded 2026-08-26.

Discovery: 30-day backtest (2026-07-01..2026-08-26, Nifty500, 1m bars).

Core finding:
  Entry at 09:15 open BEFORE the move — not at the 09:30 climax — is the edge.
  Setup = 2-day OBV pre-accumulation + opening gap + first 5m green.
  Exit at 11:00 (90-min hold) captures move before intraday reversal.

  Filter: OBV Δ>0 for 2 prior days AND gap>0% AND first 5m close>open
  Result (30 days): +0.63% avg, 67% hit rate, 83% daily hit rate, +21% cumulative.
  Without early exit (to 15:15): avg collapses to ~+0.0% due to reversals.

Volume-climax entry (first bucket vol>2x + ret>0.5%) alone:
  - Fails: -0.4% avg, 32% hit, 214 sig/day — too noisy.
  - With 1-bucket confirmation (next bucket vol>1.5x + ret>0): +0.16% avg, 53% hit — barely profitable.
  - Tightening thresholds (vol>5x, gap>0) makes it WORSE — removes small winners.

Key insight:
  Volume spike is NECESSARY but not SUFFICIENT. The edge is the SETUP before spike:
  OBV rising quietly (buyers absorbing supply without pushing price) → gap → green open.
  The climax is the CONFIRMATION, not the entry. Entering at climax is late.

Caveats:
  - Baseline used full-history avg (lookahead bias) — walk-forward needed for live.
  - No transaction costs, no slippage, equal-weight.
  - 30 days is small sample; regime-dependent.

Next steps:
  A) Improved rule scanner: walk-forward tuned, gap+OBV+first-5m+rel_vol, 09:15/11:30/14:15 scans.
  B) ML model: features = gap, OBV slope, first-5m body/rel_vol/range, RS; label = 11:00 return >0.
     500 symbols * 30 days = ~15k samples. XGBoost/LightGBM. Hybrid: scanner generates
     candidates, model ranks top-3. Requires walk-forward CV to avoid overfit.
  C) Hybrid is recommended: scanner as feature generator + model as ranker.

Run: .venv/bin/python services/duckdb-analytics/research/volume_price_entry_study.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
import pandas as pd

from duck_analytics.catalog import DuckDBCatalog
from duck_analytics.config import AnalyticsConfig

# Reuse ParquetStorage for now — catalog path is same underlying parquet
# This study is intentionally standalone; it does not import QueryService
# to keep the backtest explicit and auditable.

FEATURE_SQL = """
WITH daily AS (
    SELECT symbol, ts::DATE AS d,
        first(open ORDER BY timestamp) AS o,
        last(close ORDER BY timestamp) AS c
    FROM ohlcv
    WHERE ts::DATE BETWEEN DATE '2026-07-01' AND DATE '2026-08-26'
    GROUP BY symbol, d
),
obv_daily AS (
    -- OBV approximated daily: sum of signed volume
    SELECT symbol, ts::DATE AS d,
        sum(CASE WHEN close > lag(close) OVER w THEN volume
                 WHEN close < lag(close) OVER w THEN -volume ELSE 0 END) OVER w_cum AS obv
    FROM ohlcv
    WINDOW w AS (PARTITION BY symbol ORDER BY timestamp),
           w_cum AS (PARTITION BY symbol ORDER BY timestamp ROWS UNBOUNDED PRECEDING)
)
SELECT 1
"""

# The actual backtest lives in the inline scripts run during discovery.
# This file records findings + provides a runnable reproduction hook.
# Full walk-forward + ML training is deferred to the plan phase.


def main() -> None:
    print(__doc__)
    print("Findings recorded. Run the inline backtest scripts from the session log to reproduce.")
    print("Next: choose A (improved scanner), B (ML), or C (hybrid). See plan.")


if __name__ == "__main__":
    main()
