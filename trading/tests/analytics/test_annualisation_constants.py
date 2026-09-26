"""Pin the annualisation constants and the values they produce.

These numbers feed every Sharpe, volatility, Sortino, Calmar and
annualised-return figure the platform produces, so a well-meaning edit that
"corrects" 252 or 375 is a silent correctness regression. These tests assert
the VALUES, not just that the names exist.
"""

from __future__ import annotations

import math

import pytest

from tradex_analytics.reports import (
    SESSION_MINUTES_PER_DAY,
    TRADING_DAYS_PER_YEAR,
    _periods_per_year,
    sharpe_ratio,
)
from tradex_analytics.volatility.volatility import realized_vol

# Fixed price series used for the realized-vol pins below. Chosen before the
# rename so the expected values below are the pre-change output.
PRICES = [
    100.0, 102.5, 101.25, 103.75, 99.5, 105.0, 107.25, 106.0, 110.5, 108.75, 112.0,
]
RETURNS = [
    0.01, -0.02, 0.015, 0.0, -0.005, 0.03, -0.01, 0.022, 0.004, -0.007, 0.011,
    -0.003, 0.02,
]

# Bar length in minutes, used to re-derive the intraday annualisation factors.
_BAR_MINUTES = {"hour": 60, "30m": 30, "15m": 15, "5m": 5, "1m": 1}


class TestAnnualisationConstants:
    def test_trading_days_per_year_value(self):
        assert TRADING_DAYS_PER_YEAR == 252

    def test_session_minutes_per_day_value(self):
        assert SESSION_MINUTES_PER_DAY == 375

    def test_constants_are_ints(self):
        assert isinstance(TRADING_DAYS_PER_YEAR, int)
        assert isinstance(SESSION_MINUTES_PER_DAY, int)


class TestPeriodsPerYear:
    """Pin _periods_per_year output for every frequency in its mapping."""

    @pytest.mark.parametrize(
        ("frequency", "expected"),
        [
            ("daily", 252),
            ("1d", 252),
            ("d1", 252),
            ("1w", 52),
            ("w1", 52),
            ("weekly", 52),
            ("hour", 1575),  # 252 * (375 / 60) == 252 * 6.25
            ("1h", 1575),
            ("h1", 1575),
            ("60m", 1575),
            ("30m", 3150),  # 252 * (375 / 30) == 252 * 12.5
            ("m30", 3150),
            ("15m", 6300),  # 252 * (375 / 15) == 252 * 25
            ("m15", 6300),
            ("5m", 18900),  # 252 * (375 / 5) == 252 * 75
            ("m5", 18900),
            ("1m", 94500),  # 252 * 375
            ("m1", 94500),
        ],
    )
    def test_frequency_value(self, frequency, expected):
        assert _periods_per_year(frequency) == expected

    def test_daily_equals_trading_days(self):
        assert _periods_per_year("daily") == TRADING_DAYS_PER_YEAR

    def test_one_minute_equals_days_times_minutes(self):
        assert _periods_per_year("1m") == TRADING_DAYS_PER_YEAR * SESSION_MINUTES_PER_DAY

    def test_intraday_frequencies_are_exact_multiples(self):
        for freq in ("hour", "30m", "15m", "5m", "1m"):
            assert _periods_per_year(freq) == int(
                TRADING_DAYS_PER_YEAR * SESSION_MINUTES_PER_DAY / _BAR_MINUTES[freq]
            )

    def test_unknown_frequency_falls_back_to_daily(self):
        assert _periods_per_year("nope") == TRADING_DAYS_PER_YEAR
        assert _periods_per_year("") == TRADING_DAYS_PER_YEAR

    def test_frequency_is_case_and_whitespace_insensitive(self):
        assert _periods_per_year("DAILY") == _periods_per_year("daily")
        assert _periods_per_year(" 1m ") == _periods_per_year("1m")

    def test_frequency_accepts_non_str(self):
        assert _periods_per_year(1) == TRADING_DAYS_PER_YEAR


class TestRealizedVol:
    def test_default_matches_explicit_252(self):
        assert realized_vol(PRICES) == realized_vol(PRICES, periods_per_year=252)

    def test_default_uses_trading_days_constant(self):
        import inspect

        default = inspect.signature(realized_vol).parameters[
            "periods_per_year"
        ].default
        assert default == TRADING_DAYS_PER_YEAR

    def test_value_unchanged_by_rename(self):
        # Captured from the pre-rename implementation.
        assert realized_vol(PRICES, periods_per_year=252) == pytest.approx(
            0.47777983105504956
        )

    def test_default_value_unchanged_by_rename(self):
        assert realized_vol(PRICES) == pytest.approx(0.47777983105504956)

    def test_weekly_basis_value(self):
        assert realized_vol(PRICES, periods_per_year=52) == pytest.approx(
            0.21703471928084625
        )

    def test_scales_with_sqrt_of_periods(self):
        daily = realized_vol(PRICES, periods_per_year=252)
        weekly = realized_vol(PRICES, periods_per_year=52)
        assert daily == pytest.approx(weekly * math.sqrt(252 / 52))

    def test_too_few_prices_is_zero(self):
        assert realized_vol([]) == 0.0
        assert realized_vol([100.0]) == 0.0
        assert realized_vol([100.0, 101.0]) == 0.0


class TestSharpeAnnualisation:
    def test_daily_value_unchanged_by_rename(self):
        assert sharpe_ratio(RETURNS) == pytest.approx(5.6868719268387355)

    def test_one_minute_value_unchanged_by_rename(self):
        assert sharpe_ratio(RETURNS, "1m") == pytest.approx(110.12580132330453)


class TestEngineAnnualisation:
    def test_tearsheet_values_unchanged_by_rename(self):
        from tradex_analytics.engine import AnalyticsEngine

        sheet = AnalyticsEngine().tearsheet(RETURNS)
        assert sheet["volatility"] == pytest.approx(0.2283802497186183)
        assert sheet["sortino_ratio"] == pytest.approx(7.57674106634215)
        assert sheet["calmar_ratio"] == pytest.approx(64.93846153846154)
        assert sheet["sharpe_ratio"] == pytest.approx(5.6868719268387355)
