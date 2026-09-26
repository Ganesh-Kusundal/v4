"""Analytics module — technical indicators and performance reports."""

from tradex_analytics.breadth import advance_decline
from tradex_analytics.engine import AnalyticsEngine
from tradex_analytics.footprint import Footprint
from tradex_analytics.indicators import ema, macd, roc, rsi, sma
from tradex_analytics.orderflow import classify_aggressor, cvd_from_quotes, imbalance
from tradex_analytics.probability import win_rate
from tradex_analytics.reports import max_drawdown, sharpe_ratio, sortino_ratio, total_return
from tradex_analytics.trade_metrics import (
    Statistics,
    Trade,
    compute_statistics,
    round_trip_trades,
)
from tradex_analytics.volatility.volatility import realized_vol
from tradex_analytics.volume.volume_profile import lvn, poc, vah, val, value_area

