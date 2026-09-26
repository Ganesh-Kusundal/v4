"""Point-in-time (PIT) validators for research runs.

These are correctness guards, not data transforms.  A PIT violation means the
research result is contaminated by future information and cannot be trusted.

Five invariants enforced:

1. ``assert_no_future_candle`` — feature bar timestamp ≤ decision timestamp
   (look-ahead bias via a bar that hasn't closed yet)
2. ``assert_feature_ts_leq_decision_ts`` — pipeline-friendly alias for (1)
3. ``assert_historical_universe`` — dataset symbols ⊆ manifest universe
   (survivorship bias via a constituent that only entered the index later)
4. ``assert_corporate_actions_timing`` — no corporate action with ex_date
   after the reference date is applied (future split/bonus bias)
5. ``assert_price_series_labelled`` — dataset manifest explicitly declares
   "raw" or "adjusted" (silent mixing is one of the most common research bugs)

All violations raise ``PitViolation`` (a ``ValueError`` subclass) with a
description naming the first offending value so the caller can halt cleanly.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import pandas as pd

from .manifests import DatasetManifest, UniverseManifest


class PitViolation(ValueError):
    """Raised when a point-in-time contract is violated."""


def assert_no_future_candle(
    df: pd.DataFrame,
    *,
    feature_ts_col: str,
    decision_ts_col: str,
) -> None:
    """Assert every feature row's bar timestamp ≤ the decision timestamp.

    A decision made at time T can only use bars whose close timestamp is ≤ T.
    A bar that closes *after* T is a future candle — look-ahead bias.

    :param df: DataFrame with at least ``feature_ts_col`` and ``decision_ts_col``.
    :param feature_ts_col: Column name of the feature computation timestamp.
    :param decision_ts_col: Column name of the signal/decision timestamp.
    :raises PitViolation: on the first row where ``feature_ts > decision_ts``.
    """
    if df.empty:
        return
    bad = df[df[feature_ts_col] > df[decision_ts_col]]
    if not bad.empty:
        first = bad.iloc[0]
        raise PitViolation(
            f"future candle detected: feature_ts={first[feature_ts_col]} "
            f"> decision_ts={first[decision_ts_col]} at row index {bad.index[0]}"
        )


def assert_feature_ts_leq_decision_ts(
    df: pd.DataFrame,
    *,
    feature_ts_col: str,
    decision_ts_col: str,
) -> None:
    """Pipeline-friendly alias for ``assert_no_future_candle``.

    Use this name in feature pipeline checkpoints to make the intent explicit.
    """
    assert_no_future_candle(df, feature_ts_col=feature_ts_col, decision_ts_col=decision_ts_col)


def assert_historical_universe(
    manifest: UniverseManifest,
    symbols_in_dataset: Sequence[str],
) -> None:
    """Assert every symbol in the dataset was in the universe at ``manifest.as_of_date``.

    Catches survivorship bias: if the dataset includes a symbol that only
    entered the index *after* ``as_of_date``, research results for the period
    before entry are contaminated by look-ahead knowledge of that symbol.

    :param manifest: Universe manifest recording the PIT constituency.
    :param symbols_in_dataset: Symbols actually used in the research run.
    :raises PitViolation: if any dataset symbol is absent from the manifest.
    """
    manifest_set = set(manifest.symbols)
    extra = set(symbols_in_dataset) - manifest_set
    if extra:
        raise PitViolation(
            f"dataset contains symbols not in universe manifest "
            f"(as_of={manifest.as_of_date}): {sorted(extra)}"
        )


def assert_corporate_actions_timing(
    symbol: str,
    actions: list[dict],
    *,
    reference_date: date,
) -> None:
    """Assert no corporate action with ex_date > reference_date is applied.

    A split or bonus with ex_date D should not adjust historical prices for
    any date before D.  Applying a future corporate action retroactively
    inflates/deflates historical returns and is a PIT violation.

    :param symbol: Instrument symbol (used in the violation message).
    :param actions: List of dicts with at least ``ex_date`` (``date`` or ISO
        string) and ``action_type``.
    :param reference_date: The research run's as-of date / cutoff.
    :raises PitViolation: if any action's ex_date is strictly after reference_date.
    """
    for action in actions:
        ex_date = action.get("ex_date")
        if ex_date is None:
            continue
        if isinstance(ex_date, str):
            ex_date = date.fromisoformat(ex_date)
        if ex_date > reference_date:
            raise PitViolation(
                f"corporate action for {symbol} has ex_date={ex_date} "
                f"which is after reference_date={reference_date}; "
                f"applying this adjustment is a PIT violation"
            )


def assert_price_series_labelled(manifest: DatasetManifest) -> None:
    """Assert the dataset manifest explicitly declares 'raw' or 'adjusted'.

    Silently mixing raw and adjusted prices is one of the most common research
    errors: raw prices contain split gaps that produce phantom alpha; adjusted
    prices applied with a future corporate action cutoff introduce look-ahead.
    This guard is a callable pipeline checkpoint — call it at dataset ingestion
    and at every join boundary.

    ``DatasetManifest.__post_init__`` already enforces the same rule at
    construction time; this function gives pipeline stages an explicit,
    import-by-name guard.

    :raises PitViolation: if ``manifest.price_series`` is not 'raw' or 'adjusted'.
    """
    if manifest.price_series not in ("raw", "adjusted"):
        raise PitViolation(
            f"price_series must be 'raw' or 'adjusted', got {manifest.price_series!r}"
        )


__all__ = [
    "PitViolation",
    "assert_corporate_actions_timing",
    "assert_feature_ts_leq_decision_ts",
    "assert_historical_universe",
    "assert_no_future_candle",
    "assert_price_series_labelled",
]
