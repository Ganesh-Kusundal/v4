"""Tests for breadth indicator, TRIN, and existing advance_decline."""

from __future__ import annotations

import pytest

from tradex_trading.analytics.breadth import advance_decline, breadth_indicator, trin


class TestBreadthIndicator:
    def test_breadth_indicator_basic(self) -> None:
        # 3 symbols, 2 periods
        data = {
            "AAPL": [0.01, -0.02],
            "GOOG": [0.03, 0.01],
            "MSFT": [-0.01, -0.03],
        }
        result = breadth_indicator(data)
        assert len(result) == 2
        # Period 0: AAPL +, GOOG +, MSFT - → advances=2, declines=1
        assert result[0]["period"] == 0
        assert result[0]["advances"] == 2
        assert result[0]["declines"] == 1
        assert result[0]["ratio"] == pytest.approx(2.0)
        assert result[0]["cumulative"] == pytest.approx(2.0)
        # Period 1: AAPL -, GOOG +, MSFT - → advances=1, declines=2
        assert result[1]["period"] == 1
        assert result[1]["advances"] == 1
        assert result[1]["declines"] == 2
        assert result[1]["ratio"] == pytest.approx(0.5)
        assert result[1]["cumulative"] == pytest.approx(1.0)  # 2.0 * 0.5

    def test_breadth_indicator_empty(self) -> None:
        assert breadth_indicator({}) == []

    def test_breadth_indicator_single_symbol(self) -> None:
        data = {"AAPL": [0.05, -0.03, 0.01]}
        result = breadth_indicator(data)
        assert len(result) == 3
        # Single symbol: each period is either 1/0 or 0/1
        assert result[0]["advances"] == 1
        assert result[0]["declines"] == 0
        assert result[0]["ratio"] == float("inf")
        assert result[1]["advances"] == 0
        assert result[1]["declines"] == 1
        assert result[2]["advances"] == 1
        assert result[2]["declines"] == 0


class TestTrin:
    def test_trin_basic(self) -> None:
        # TRIN = (10/5) / (200/100) = 2.0 / 2.0 = 1.0
        assert trin(5, 10, 100.0, 200.0) == pytest.approx(1.0)

    def test_trin_zero_advances(self) -> None:
        assert trin(0, 10, 100.0, 200.0) == float("inf")


class TestAdvanceDeclineUnchanged:
    def test_advance_decline_unchanged(self) -> None:
        advances, declines, ratio = advance_decline([0.01, -0.02, 0.0, 0.03])
        assert advances == 2
        assert declines == 1
        assert ratio == pytest.approx(2.0)

