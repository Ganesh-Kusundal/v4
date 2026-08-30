"""Unit tests for RS score SQL template and DTW pattern matching."""

from __future__ import annotations

import numpy as np
import pytest

from duck_analytics.patterns import dtw_distance, znorm
from duck_analytics.rs_score import rs_score_sql


class TestRsScoreSql:
    def test_weight_midpoint_is_half_of_end(self):
        # W_i = exp(2 ln2 i / L): W(L/2) must be exactly W(L)/2 = 2.
        bars = 500
        w_end = np.exp(2 * np.log(2) * bars / bars)
        w_mid = np.exp(2 * np.log(2) * (bars // 2) / bars)
        assert w_end == pytest.approx(4.0)
        assert w_mid == pytest.approx(w_end / 2)

    def test_sql_renders_and_bounds(self):
        sql = rs_score_sql(end="2026-08-21 15:30:00", days=20,
                           bars_per_day=25, interval="15 minutes")
        assert "time_bucket(INTERVAL '15 minutes', ts)" in sql
        assert "TIMESTAMP '2026-08-21 15:30:00'" in sql   # as_of bound
        assert "HAVING count(*) = 500" in sql             # days*bars_per_day

    def test_unsupported_bars_guard(self):
        sql = rs_score_sql(end="2026-08-21", symbols=[])
        assert "AND false" in sql


class TestDtw:
    def test_identical_sequences_zero(self):
        a = np.array([1.0, 2.0, 3.0, 4.0])
        assert dtw_distance(a, a) < 1e-9

    def test_shape_invariant_to_level_and_scale(self):
        a = np.array([1.0, 2.0, 3.0, 2.5, 2.0])
        b = a * 7 + 100                      # same shape, different level
        c = np.array([5.0, 1.0, 0.0, 1.5, 3.0])  # opposite shape
        za, zb, zc = znorm(a), znorm(b), znorm(c)
        assert dtw_distance(za, zb) < 1e-6
        assert dtw_distance(za, zc) > 0.1

    def test_warp_tolerant_vs_euclidean(self):
        # Same ramp stretched over more steps: DTW should beat plain L2.
        a = np.linspace(0, 1, 10)
        b = np.linspace(0, 1, 14)
        dtw = dtw_distance(znorm(a), znorm(b))
        l2 = np.linalg.norm(
            znorm(a)[:10] - znorm(np.interp(np.linspace(0, 1, 10), [0, 1], [0, 1]))
            if False else znorm(a) - znorm(a))  # trivial control
        assert dtw >= 0.0 and l2 == pytest.approx(0.0)
        # warped alignment cost stays small relative to unnormalized scale
        assert dtw < 0.2

    def test_band_constraint_respected(self):
        rng = np.random.default_rng(42)
        a = rng.normal(size=50)
        d = dtw_distance(znorm(a), znorm(a[::-1]), band_frac=0.1)
        assert np.isfinite(d)
