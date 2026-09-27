"""Tests for the research-validity primitives: deflated Sharpe and grid stability.

These are the two numbers that make a grid search's "best cell" honest:

* :func:`deflated_sharpe` — the observed Sharpe minus the expected maximum
  Sharpe of ``n_trials`` independent trials drawn at the observed dispersion,
* :func:`param_stability_share_of_grid` — the fraction of the whole grid sitting
  within one Sharpe unit of the optimum.

Every expected value below is re-derived inside the test from the published
formula rather than copied from the implementation, so a change in either one
breaks the other.
"""

from __future__ import annotations

import math
from statistics import NormalDist

import pytest
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

_GAMMA = EULER_MASCHERONI


def _sr_star_reference(n_trials: int, variance: float) -> float:
    """The published SR* term, written out longhand as a test oracle.

    Bailey & Lopez de Prado, "The Deflated Sharpe Ratio" (2014):

        SR* = sqrt(V[SR_n]) * [ (1 - g) Z^-1(1 - 1/N) + g Z^-1(1 - 1/(N e)) ]
    """
    if n_trials < 2:
        return 0.0
    z = NormalDist()
    return math.sqrt(variance) * (
        (1 - _GAMMA) * z.inv_cdf(1 - 1 / n_trials)
        + _GAMMA * z.inv_cdf(1 - 1 / (n_trials * math.e))
    )


def _sample_variance(values: list[float]) -> float:
    mean = sum(values) / len(values)
    return sum((v - mean) ** 2 for v in values) / (len(values) - 1)


# ---------------------------------------------------------------------------
# expected_max_sharpe
# ---------------------------------------------------------------------------


class TestExpectedMaxSharpe:
    def test_matches_the_published_formula(self):
        assert expected_max_sharpe(9, 0.145) == pytest.approx(
            _sr_star_reference(9, 0.145), abs=1e-12
        )

    def test_known_value_at_nine_trials(self):
        # sqrt(0.145) * [(1-g)Z^-1(8/9) + g Z^-1(1 - 1/(9e))] = 0.5790951
        assert expected_max_sharpe(9, 0.145) == pytest.approx(0.5790951, abs=1e-6)

    def test_one_trial_has_no_selection_bias(self):
        # A single cell was never selected *from* anything, so nothing to deflate.
        assert expected_max_sharpe(1, 0.5) == 0.0
        assert expected_max_sharpe(0, 0.5) == 0.0

    def test_zero_dispersion_has_no_expected_max(self):
        # Every trial scored identically: the max is that score, not above it.
        assert expected_max_sharpe(500, 0.0) == 0.0

    def test_scales_with_the_square_root_of_dispersion(self):
        assert expected_max_sharpe(9, 0.04) == pytest.approx(
            2.0 * expected_max_sharpe(9, 0.01), rel=1e-12
        )

    def test_grows_with_the_trial_count(self):
        values = [expected_max_sharpe(n, 0.2) for n in (2, 5, 10, 50, 500, 5000)]
        assert values == sorted(values)
        assert values[0] > 0.0

    def test_trial_count_enters_through_both_normal_quantiles(self):
        # A grid that is merely large must still deflate: dropping either
        # Z^-1 term would leave N out of the formula.
        assert expected_max_sharpe(1000, 0.2) != pytest.approx(
            expected_max_sharpe(2, 0.2)
        )


# ---------------------------------------------------------------------------
# deflated_sharpe
# ---------------------------------------------------------------------------


class TestDeflatedSharpe:
    def test_value_is_observed_minus_expected_max(self):
        result = deflated_sharpe([1.0, 0.2, -0.3, 0.5, 0.1, -0.1, 0.4, 0.0, 0.3])
        assert isinstance(result, DeflatedSharpe)
        assert result.observed == 1.0
        assert result.n_trials == 9
        assert result.sharpe_variance == pytest.approx(0.145, abs=1e-12)
        assert result.expected_max == pytest.approx(0.5790951, abs=1e-6)
        assert result.value == pytest.approx(1.0 - 0.5790951, abs=1e-6)

    def test_defaults_to_the_best_trial(self):
        assert deflated_sharpe([0.4, 1.2, 0.9]).observed == 1.2

    def test_accepts_an_explicit_observed_value(self):
        result = deflated_sharpe([0.4, 0.9, 0.1], observed=0.5)
        assert result.observed == 0.5
        assert result.expected_max == pytest.approx(expected_max_sharpe(3, _sample_variance(
            [0.4, 0.9, 0.1])))

    def test_wider_grid_deflates_the_same_sharpe_harder(self):
        # Same observed Sharpe, same dispersion, more cells tried: the reported
        # number has to fall, or the trial count is decorative.
        nine = deflated_sharpe([1.0, 0.2, -0.3, 0.5, 0.1, -0.1, 0.4, 0.0, 0.3])
        wide = deflated_sharpe([1.0, 0.2, -0.3, 0.5, 0.1, -0.1, 0.4, 0.0, 0.3] * 100)
        assert wide.n_trials == 900
        assert wide.value < nine.value

    def test_a_wide_noisy_grid_deflates_below_zero(self):
        # The overfitting signature the correction exists for: 2001 cells
        # scattered over [-1, 1] whose winner is 1.8. The best cell is only ~3.1
        # standard deviations above the field, while the best of 2001 draws is
        # expected to be ~3.6 above it, so the reported excess is negative and a
        # `deflated_sharpe >= 0.0` bar refuses the result.
        spread = [-1.0 + 2.0 * i / 1999 for i in range(2000)]
        result = deflated_sharpe([*spread, 1.8])
        assert result is not None
        assert result.n_trials == 2001
        assert result.observed == 1.8
        assert result.expected_max == pytest.approx(1.996194, abs=1e-5)
        assert result.value == pytest.approx(-0.196194, abs=1e-5)
        assert result.value < 0.0

    def test_a_lone_outlier_is_not_itself_deflated(self):
        # One peak among identical cells carries almost all the dispersion, so
        # SR* stays well under it. Overfitting looks like a peak that is *not*
        # extreme relative to the field, not like an isolated outlier.
        result = deflated_sharpe([0.6] * 29 + [1.2])
        assert result is not None
        assert result.value > 0.0

    def test_single_trial_has_nothing_to_deflate(self):
        # Dispersion is undefined for one observation. None, not 0.0 — a zero
        # here would satisfy a `deflated_sharpe >= 0.0` bar vacuously.
        assert deflated_sharpe([1.5]) is None

    def test_no_trials_is_none(self):
        assert deflated_sharpe([]) is None

    def test_identical_sharpes_deflate_to_nothing(self):
        result = deflated_sharpe([1.0] * 20)
        assert result.expected_max == 0.0
        assert result.value == 1.0

    def test_is_never_above_the_observed_sharpe(self):
        result = deflated_sharpe([3.0, -1.0, 0.5, 2.0, -2.5, 1.25, 0.75, 2.5])
        assert result.value <= result.observed


# ---------------------------------------------------------------------------
# param_stability_share_of_grid
# ---------------------------------------------------------------------------


class TestParamStabilityShare:
    def test_exact_share_on_a_known_grid(self):
        scores = [3.0, 2.5, 2.1, 1.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        assert param_stability_share_of_grid(scores) == pytest.approx(0.3)

    def test_the_optimum_counts_as_being_within_one_unit_of_itself(self):
        assert param_stability_share_of_grid([3.0, 0.0, 0.0]) == pytest.approx(1 / 3)

    def test_the_tolerance_boundary_is_inclusive(self):
        # optimum 3.0, tolerance 1.0 -> a cell at exactly 2.0 is inside it.
        assert param_stability_share_of_grid([3.0, 2.0, 1.999]) == pytest.approx(2 / 3)

    def test_tolerance_is_one_sharpe_unit(self):
        assert PARAM_STABILITY_TOLERANCE == 1.0
        assert param_stability_share_of_grid([2.0, 1.0, 0.999]) == pytest.approx(2 / 3)

    def test_a_plateau_beats_a_spike_of_the_same_peak(self):
        spike = [3.0, 1.9, 1.8, 1.7, 1.6]
        plateau = [3.0, 2.9, 2.8, 2.7, 2.6]
        assert param_stability_share_of_grid(spike) == pytest.approx(0.2)
        assert param_stability_share_of_grid(plateau) == pytest.approx(1.0)

    def test_failed_cells_sit_in_the_denominator_not_the_numerator(self):
        # A cell that crashed is not evidence of a plateau, and a grid that lost
        # half its cells to exceptions must not read as a stable one. Optimum
        # 3.0 with a 1.0 tolerance puts 3.0/2.5/2.1 inside and 1.5 outside.
        scores = [3.0, 2.5, 2.1, 1.5]
        assert param_stability_share_of_grid(scores) == pytest.approx(0.75)
        assert param_stability_share_of_grid(scores, total_cells=8) == pytest.approx(0.375)

    def test_denominator_defaults_to_the_survivors(self):
        # Optimum 1.0, tolerance 1.0 -> the share counts anything at or above
        # 0.0, so 0.5 is inside and -0.5 is out.
        assert param_stability_share_of_grid([1.0, 0.5]) == pytest.approx(1.0)
        assert param_stability_share_of_grid([1.0, -0.5]) == pytest.approx(0.5)

    def test_no_scores_is_none_not_zero(self):
        # Negative control: 0.0 would clear a `>= 0.60` bar for a grid that
        # produced nothing at all.
        assert param_stability_share_of_grid([]) is None

    def test_all_identical_scores_is_a_full_share(self):
        assert param_stability_share_of_grid([0.7] * 8) == pytest.approx(1.0)

    def test_a_grid_of_one_is_a_full_share(self):
        assert param_stability_share_of_grid([1.0]) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# require_min_oos_bars
# ---------------------------------------------------------------------------


class TestRequireMinOosBars:
    def test_the_minimum_is_thirty_bars(self):
        assert MIN_OOS_BARS_PER_FOLD == 30

    def test_exactly_thirty_passes(self):
        assert require_min_oos_bars(30) == 30

    def test_twenty_nine_raises(self):
        with pytest.raises(InsufficientSampleError):
            require_min_oos_bars(29)

    def test_the_message_names_the_fold_the_count_and_the_minimum(self):
        with pytest.raises(InsufficientSampleError) as exc:
            require_min_oos_bars(12, fold="fold 3")
        message = str(exc.value)
        assert "fold 3" in message
        assert "12" in message
        assert str(MIN_OOS_BARS_PER_FOLD) in message

    def test_zero_and_negative_raise(self):
        for bars in (0, -1):
            with pytest.raises(InsufficientSampleError):
                require_min_oos_bars(bars)

    def test_is_a_value_error(self):
        # Callers that already catch ValueError around a fold keep working.
        assert issubclass(InsufficientSampleError, ValueError)

    def test_the_minimum_is_overridable(self):
        assert require_min_oos_bars(10, minimum=10) == 10
        with pytest.raises(InsufficientSampleError):
            require_min_oos_bars(9, minimum=10)
