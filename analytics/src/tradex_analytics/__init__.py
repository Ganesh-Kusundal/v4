"""Analytics module — technical indicators and performance reports."""

from tradex_analytics.breadth import advance_decline
from tradex_analytics.engine import AnalyticsEngine
from tradex_analytics.footprint import Footprint
from tradex_analytics.indicators import ema, macd, roc, rsi, sma
from tradex_analytics.orderflow import classify_aggressor, cvd_from_quotes, imbalance
from tradex_analytics.probability import win_rate
from tradex_analytics.reports import max_drawdown, sharpe_ratio, sortino_ratio, total_return
from tradex_analytics.research_validity import (
    EULER_MASCHERONI,
    MIN_OOS_BARS_PER_FOLD,
    PARAM_STABILITY_TOLERANCE,
    DeflatedSharpe,
    InsufficientSampleError,
    deflated_sharpe,
    expected_max_sharpe,
    param_stability_share_of_grid,
    require_min_oos_bars,
)
from tradex_analytics.trade_metrics import (
    Statistics,
    Trade,
    compute_statistics,
    round_trip_trades,
)
from tradex_analytics.validation_report import (
    COST_STRESS_MULTIPLIER,
    GATING_FIELDS,
    MIN_DEFATED_SHARPE,
    MIN_OOS_ROUND_TRIPS,
    MIN_OOS_SHARPE,
    MIN_OOS_SPAN_DAYS,
    MIN_PARAM_STABILITY_SHARE,
    PROMOTION_CRITERIA,
    REPORT_FIELDS,
    PromotionCriterion,
    TradeValidationReport,
    ValidationReportError,
    concatenated_oos_sharpe,
    count_round_trips,
    oos_fills_in,
    parameter_set_hash,
    round_trips_from_fills,
)
from tradex_analytics.volatility.volatility import realized_vol
from tradex_analytics.volume.volume_profile import lvn, poc, vah, val, value_area

__all__ = [
    "COST_STRESS_MULTIPLIER",
    "EULER_MASCHERONI",
    "GATING_FIELDS",
    "MIN_DEFATED_SHARPE",
    "MIN_OOS_BARS_PER_FOLD",
    "MIN_OOS_ROUND_TRIPS",
    "MIN_OOS_SHARPE",
    "MIN_OOS_SPAN_DAYS",
    "MIN_PARAM_STABILITY_SHARE",
    "PARAM_STABILITY_TOLERANCE",
    "PROMOTION_CRITERIA",
    "REPORT_FIELDS",
    "AnalyticsEngine",
    "DeflatedSharpe",
    "Footprint",
    "InsufficientSampleError",
    "PromotionCriterion",
    "Statistics",
    "Trade",
    "TradeValidationReport",
    "ValidationReportError",
    "advance_decline",
    "classify_aggressor",
    "compute_statistics",
    "concatenated_oos_sharpe",
    "count_round_trips",
    "cvd_from_quotes",
    "deflated_sharpe",
    "ema",
    "expected_max_sharpe",
    "imbalance",
    "lvn",
    "macd",
    "max_drawdown",
    "oos_fills_in",
    "param_stability_share_of_grid",
    "parameter_set_hash",
    "poc",
    "realized_vol",
    "require_min_oos_bars",
    "roc",
    "round_trip_trades",
    "round_trips_from_fills",
    "rsi",
    "sharpe_ratio",
    "sma",
    "sortino_ratio",
    "total_return",
    "vah",
    "val",
    "value_area",
    "win_rate",
]

