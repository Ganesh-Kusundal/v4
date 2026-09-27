"""Research validity — the corrections that make a grid search's best cell honest.

A grid search reports the maximum of N scores. That maximum is biased upward by
the search itself: the more cells were tried, the more the winner's score is
expected to exceed the truth by chance alone. Two numbers here say so out loud.

**Deflated Sharpe.** The observed Sharpe minus the expected *maximum* Sharpe of
``n_trials`` independent trials drawn at the observed dispersion, so a number is
comparable across grids of different sizes. The expected maximum is the term
Bailey and Lopez de Prado give as ``SR*`` in "The Deflated Sharpe Ratio:
Correcting for Selection Bias, Backtest Overfitting, and Non-Normality" (Journal
of Portfolio Management, 2014):

.. code-block:: text

    SR*   = sqrt(V[SR_n]) * [ (1 - g) * Z^-1(1 - 1/N) + g * Z^-1(1 - 1/(N*e)) ]
    DS    = SR_observed - SR*

with ``g`` the Euler-Mascheroni constant (0.5772156649...), ``Z^-1`` the inverse
standard normal CDF, ``N`` the number of trials and ``V[SR_n]`` the sample
variance of the trial Sharpe ratios.

Two deliberate choices, both stated because a reader should be able to disagree
with them:

* **The reported quantity is on the Sharpe scale, not a probability.** The paper
  also defines a probability form, ``Z((SR - SR*)sqrt(T-1) / sqrt(1 - g3*SR +
  ((g4-1)/4)*SR^2))``. That form needs the skewness and kurtosis of the selected
  strategy's *returns*, which a grid search over scores does not carry, so it is
  not computed here. It would also be useless to the promotion bar as written: a
  probability lives in [0, 1], so a criterion of ``deflated_sharpe >= 0.0`` would
  be satisfied by everything. ``SR_observed - SR*`` is on the same scale as the
  raw Sharpe and can be gated.
* **A trial is a cell that produced a score.** A cell that crashed was never a
  candidate for the maximum, so it does not inflate ``SR*``. It is not laundered
  either: it is counted, it is in the denominator of
  :func:`param_stability_share_of_grid`, and it appears in the grid result's
  failure list.

**Parameter stability.** :func:`param_stability_share_of_grid` is the fraction of
the grid scoring within one Sharpe unit of the optimum. The promotion bar wants a
plateau; a fitted result is a spike with every neighbour much worse, and no
Sharpe correction describes that shape as plainly as the surface itself does.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist

#: Euler-Mascheroni constant. Appears in both terms of the SR* bracket: the
#: paper's blend of a finite-sample correction and its asymptotic limit.
EULER_MASCHERONI: float = 0.5772156649015329

#: How far from the optimum still counts as "the same result", in Sharpe units.
#: The promotion bar's ``param_stability_share_of_grid >= 0.60`` is defined
#: against this number, so it is named rather than passed around as a literal.
PARAM_STABILITY_TOLERANCE: float = 1.0

#: Minimum out-of-sample bars a fold may be judged on. Below this a Sharpe is
#: mostly sampling noise, and a "best" cell chosen off a handful of bars is not
#: a result. 30 is the value the build spec names for the walk-forward folds.
MIN_OOS_BARS_PER_FOLD: int = 30

__all__ = [
    "EULER_MASCHERONI",
    "MIN_OOS_BARS_PER_FOLD",
    "PARAM_STABILITY_TOLERANCE",
    "DeflatedSharpe",
    "InsufficientSampleError",
    "deflated_sharpe",
    "expected_max_sharpe",
    "param_stability_share_of_grid",
    "require_min_oos_bars",
]


class InsufficientSampleError(ValueError):
    """A fold or cell was judged on fewer bars than the minimum allows.

    A ``ValueError`` so a caller already guarding a fold's arguments keeps
    working unchanged.
    """


@dataclass(frozen=True, slots=True)
class DeflatedSharpe:
    """The deflation of one observed Sharpe against its own grid.

    Attributes
    ----------
    n_trials:
        Cells that produced a score, i.e. the ``N`` in the SR* bracket.
    sharpe_variance:
        ``V[SR_n]``, the unbiased sample variance of those trial Sharpes.
    expected_max:
        ``SR*``, the Sharpe the best of ``n_trials`` such trials is expected to
        reach with no edge at all.
    observed:
        The Sharpe being deflated, in the same units as the trial Sharpes.
    value:
        ``observed - expected_max``: the excess over what pure selection buys.
    """

    n_trials: int
    sharpe_variance: float
    expected_max: float
    observed: float
    value: float


def _sample_variance(values: Sequence[float]) -> float:
    """Unbiased sample variance. ``values`` must hold at least two entries."""
    mean = sum(values) / len(values)
    return sum((v - mean) ** 2 for v in values) / (len(values) - 1)


def expected_max_sharpe(n_trials: int, sharpe_variance: float) -> float:
    """The expected maximum Sharpe of ``n_trials`` trials at that dispersion.

    ``SR*`` from the Deflated Sharpe paper: the square root of the trial
    Sharpe variance times a blend of the finite-sample and asymptotic expected
    maximum of ``N`` standard normals, where ``g`` is the Euler-Mascheroni
    constant.

    ``n_trials < 2`` and a zero variance both return ``0.0``: a lone cell was
    never selected *from* a set, and trials that all score identically have a
    maximum equal to that score.
    """
    if n_trials < 2 or sharpe_variance <= 0.0:
        return 0.0
    normal = NormalDist()
    finite = normal.inv_cdf(1.0 - 1.0 / n_trials)
    asymptotic = normal.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    bracket = (1.0 - EULER_MASCHERONI) * finite + EULER_MASCHERONI * asymptotic
    return math.sqrt(sharpe_variance) * bracket


def deflated_sharpe(
    trial_sharpes: Sequence[float],
    *,
    observed: float | None = None,
) -> DeflatedSharpe | None:
    """Deflate the best of ``trial_sharpes`` against the grid it was chosen from.

    Parameters
    ----------
    trial_sharpes:
        One Sharpe per cell that produced a score, all on the same scale and
        the same annualisation.
    observed:
        The Sharpe to deflate. Defaults to ``max(trial_sharpes)``, which is what
        a grid search selects.

    Returns
    -------
    DeflatedSharpe | None
        ``None`` when there are fewer than two trials, because the dispersion the
        correction depends on is undefined for one observation. ``None`` and not
        ``0.0``: a zero would satisfy a ``deflated_sharpe >= 0.0`` criterion
        without having been computed.
    """
    if len(trial_sharpes) < 2:
        return None
    variance = _sample_variance(trial_sharpes)
    ceiling = expected_max_sharpe(len(trial_sharpes), variance)
    seen = max(trial_sharpes) if observed is None else observed
    return DeflatedSharpe(
        n_trials=len(trial_sharpes),
        sharpe_variance=variance,
        expected_max=ceiling,
        observed=seen,
        value=seen - ceiling,
    )


def param_stability_share_of_grid(
    scores: Sequence[float],
    *,
    tolerance: float = PARAM_STABILITY_TOLERANCE,
    total_cells: int | None = None,
) -> float | None:
    """Fraction of the grid scoring within ``tolerance`` of the optimum.

    A plateau scores near its peak across most of the surface and returns a high
    share; a fitted spike returns a low one. Read it as the shape of the surface,
    not as a score: two grids with the same peak and the same dispersion can sit
    at either end of this range.

    Parameters
    ----------
    scores:
        One score per cell that produced a value, on the Sharpe scale the
        tolerance is quoted in.
    tolerance:
        Distance from the optimum that still counts. The boundary is inclusive,
        so a cell exactly ``tolerance`` below the optimum is inside the share.
    total_cells:
        Denominator. Defaults to ``len(scores)``, but a caller that knows cells
        crashed should pass the **whole** grid size: a cell that crashed is not
        evidence of a plateau, and counting it only in the numerator would let a
        half-crashing grid read as a stable one.

    Returns
    -------
    float | None
        ``None`` when there are no scores, because there is no optimum to measure
        against. ``None`` and not ``0.0``, which would clear a
        ``>= 0.60`` criterion for a grid that produced nothing at all.
    """
    if not scores:
        return None
    optimum = max(scores)
    floor = optimum - tolerance
    within = sum(1 for s in scores if s >= floor)
    denominator = total_cells if total_cells is not None else len(scores)
    if denominator <= 0:
        return None
    return within / denominator


def require_min_oos_bars(
    n_bars: int,
    *,
    fold: str | None = None,
    minimum: int = MIN_OOS_BARS_PER_FOLD,
) -> int:
    """Assert a fold is judged on at least ``minimum`` out-of-sample bars.

    Returns ``n_bars`` when it passes, so the call can wrap an expression.

    Raises
    ------
    InsufficientSampleError
        Naming the fold, the count and the minimum, so a failed report says which
        fold was too short rather than just that something was.
    """
    if n_bars >= minimum:
        return n_bars
    where = f"fold {fold}: " if fold else ""
    raise InsufficientSampleError(
        f"{where}{n_bars} out of sample bars is below the minimum of {minimum}. "
        "A best cell chosen on fewer bars than this is noise, not a result."
    )
