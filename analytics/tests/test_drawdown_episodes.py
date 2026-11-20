"""Drawdown-episode decomposition (risk-x-ray building block).

Ported from Vibe-Trading's ``drawdown_distribution_analysis`` (MIT) into a
pure-Python, dependency-free form. An *episode* is one peak-to-recovery decline:
peak index, trough index, recovery index (None if unrecovered), depth as a
positive fraction, and duration in bars. Aggregates (ulcer/pain index, averages)
summarise the whole path.
"""

from __future__ import annotations

import math

import pytest
from tradex_analytics.reports import drawdown_episodes


def test_single_episode_recovers() -> None:
    # peak 120@1, trough 90@2, recovery 130@4.
    result = drawdown_episodes([100, 120, 90, 110, 130])
    assert result["episode_count"] == 1
    assert result["max_drawdown"] == pytest.approx(0.25)
    ep = result["episodes"][0]
    assert ep["peak_index"] == 1
    assert ep["trough_index"] == 2
    assert ep["recovery_index"] == 4
    assert ep["max_depth"] == pytest.approx(0.25)
    assert ep["duration"] == 3
    assert ep["recovered"] is True


def test_two_episodes() -> None:
    # 120@1 -> 90@2 -> 130@3 (recover), 130@3 -> 80@4 -> 140@5 (recover).
    result = drawdown_episodes([100, 120, 90, 130, 80, 140])
    assert result["episode_count"] == 2
    depths = [e["max_depth"] for e in result["episodes"]]
    assert depths[0] == pytest.approx(0.25)
    assert depths[1] == pytest.approx(1 - 80 / 130)
    assert all(e["recovered"] for e in result["episodes"])


def test_unrecovered_tail() -> None:
    result = drawdown_episodes([100, 120, 90])
    assert result["episode_count"] == 1
    ep = result["episodes"][0]
    assert ep["recovery_index"] is None
    assert ep["recovered"] is False
    assert ep["duration"] == 1  # (n-1) - peak_index = 2 - 1


def test_monotonic_increase_has_no_episodes() -> None:
    result = drawdown_episodes([100, 110, 120, 130])
    assert result["episode_count"] == 0
    assert result["max_drawdown"] == pytest.approx(0.0)
    assert result["episodes"] == []


def test_ulcer_and_pain_index() -> None:
    result = drawdown_episodes([100, 120, 90, 110, 130])
    # dd per bar = 1 - value/running_peak = [0, 0, .25, .08333, 0]
    assert result["pain_index"] == pytest.approx((0.25 + 1 - 110 / 120) / 5)
    expected_ulcer = math.sqrt((0.25 ** 2 + (1 - 110 / 120) ** 2) / 5)
    assert result["ulcer_index"] == pytest.approx(expected_ulcer)


def test_avg_depth_and_duration() -> None:
    result = drawdown_episodes([100, 120, 90, 130, 80, 140])
    depths = [0.25, 1 - 80 / 130]
    durations = [e["duration"] for e in result["episodes"]]
    assert result["avg_drawdown_depth"] == pytest.approx(sum(depths) / 2)
    assert result["avg_drawdown_duration"] == pytest.approx(sum(durations) / 2)


def test_max_drawdown_duration_is_longest() -> None:
    result = drawdown_episodes([100, 120, 90, 130, 80, 140])
    assert result["max_drawdown_duration"] == max(
        e["duration"] for e in result["episodes"]
    )


def test_empty_raises() -> None:
    with pytest.raises(ValueError):
        drawdown_episodes([])


def test_non_positive_raises() -> None:
    with pytest.raises(ValueError):
        drawdown_episodes([100, 0, 90])
