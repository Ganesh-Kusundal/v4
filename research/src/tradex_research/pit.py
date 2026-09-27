"""Point-in-time (PIT) validators for research runs.

These are correctness guards, not data transforms.  A PIT violation means the
research result is contaminated by future information and cannot be trusted.

Five invariants enforced:

1. ``assert_no_future_candle`` — feature bar timestamp ≤ decision timestamp
   (look-ahead bias via a bar that hasn't closed yet)
2. ``assert_feature_ts_leq_decision_ts`` — pipeline-friendly alias for (1)
3. ``assert_historical_universe`` — dataset symbols ⊆ manifest universe
   (survivorship bias via a constituent that only entered the index later),
   and, when given a window and the delisting ledger, a constituent that
   stopped existing *during* the window
4. ``assert_corporate_actions_timing`` — no corporate action with ex_date
   after the reference date is applied (future split/bonus bias)
5. ``assert_price_series_labelled`` — dataset manifest explicitly declares
   "raw" or "adjusted" (silent mixing is one of the most common research bugs)

All violations raise ``PitViolation`` (a ``ValueError`` subclass) with a
description naming the first offending value so the caller can halt cleanly.

The universe artifacts
----------------------
Two checked-in CSVs under ``Dependencies/`` carry the membership history, and
they are the *only* copy that exists: nothing in this repository holds
historical index membership, and neither configured broker will serve it for
past dates. Both therefore record a claim no weaker than what can be sourced.

``nifty{50,100,200,500}_list.csv`` carry a ``membership_asof`` column — the
date the snapshot was last refreshed, which is *not* each name's index entry
date. A snapshot answers "which names were members as of this date"; it cannot
answer "when did each name join", and inventing a per-name date is precisely
the failure this module exists to prevent.

``nse_delistings.csv`` (``symbol,delist_date,series``) is the exit-side
counterpart and is **header-only**: zero delistings could be established from
any source available here, so there are zero rows. It is not empty by
oversight. A caller that wants a point-in-time verdict must therefore treat an
empty ledger as *unverified* rather than as *no delistings* — the window check
below can only report what the ledger says, and a ledger with nothing in it
establishes nothing.
"""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

import pandas as pd

from .manifests import DatasetManifest, UniverseManifest

#: Repo root is 4 levels up from this module
#: (tradex_research → src → research → repo root).
_DEFAULT_DELISTINGS = Path(__file__).resolve().parents[3] / "Dependencies" / "nse_delistings.csv"

#: Columns the delisting ledger must carry. The ledger is keyed on the *trading
#: symbol*, which is not a unique security: ``CHOLAFIN`` and ``IMC1`` each list
#: a debenture or bond series beside the equity, so ``series`` is what tells a
#: maintainer which security a row is about, and the reader below refuses to
#: apply a row outside the cash series.
_DELISTINGS_COLUMNS = ("symbol", "delist_date", "series")

#: The cash series ``tradex_domain.universe.load_universe`` keeps. A delisting
#: outside them is a different security under the same trading symbol.
_CASH_SERIES = frozenset({"EQ", "BE"})


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


def load_delistings(path: Path | str | None = None) -> dict[str, date | None]:
    """Read the checked-in delisting ledger into ``{symbol: delist_date}``.

    ``Dependencies/nse_delistings.csv`` is hand-maintained because it has no
    other possible source. A row whose ``delist_date`` is blank records a
    symbol known to be gone whose exit date was never established; it is
    carried through as ``None`` rather than dropped, because a dropped row is
    a hole that looks like a name that never left.

    A row naming a series outside the cash segment is skipped: that is a
    debenture or bond sharing a trading symbol with an equity, and applying its
    delisting would fail a window on a name that is still listed.

    :param path: Ledger path; defaults to ``Dependencies/nse_delistings.csv``.
    :returns: ``{symbol: delist_date}``, ``None`` where the date is blank.
    :raises FileNotFoundError: if the ledger is absent.
    :raises ValueError: on a missing column or an unparseable date.
    """
    csv_path = Path(path) if path is not None else _DEFAULT_DELISTINGS
    if not csv_path.exists():
        raise FileNotFoundError(f"delisting ledger not found: {csv_path}")

    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in _DELISTINGS_COLUMNS if c not in (reader.fieldnames or ())]
        if missing:
            raise ValueError(
                f"{csv_path}: delisting ledger is missing column(s) {missing}; "
                f"expected {list(_DELISTINGS_COLUMNS)}"
            )
        delistings: dict[str, date | None] = {}
        for row in reader:
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            series = (row.get("series") or "").strip().upper()
            if series and series not in _CASH_SERIES:
                continue
            raw = (row.get("delist_date") or "").strip()
            if not raw:
                delistings[symbol] = None
                continue
            try:
                delistings[symbol] = date.fromisoformat(raw)
            except ValueError as exc:
                raise ValueError(
                    f"{csv_path}: delist_date {raw!r} for {symbol} is not an ISO date; "
                    f"a guessed date silently rewrites every backtest window spanning it"
                ) from exc
    return delistings


def assert_historical_universe(
    manifest: UniverseManifest,
    symbols_in_dataset: Sequence[str],
    *,
    window_start: date | None = None,
    window_end: date | None = None,
    delistings: Mapping[str, date | None] | None = None,
) -> None:
    """Assert the dataset's universe is point-in-time correct for a window.

    Two halves of the same bias, and the first is not sufficient:

    * **Entry (look-ahead).** Every symbol used must be in the universe as of
      ``manifest.as_of_date``. A name that joined the index afterwards is
      knowledge the run could not have had.
    * **Exit (survivorship).** When a *window* and the delisting ledger are
      supplied, no symbol may have been delisted inside it. A name that stopped
      existing mid-window means the composition at the window's open differs
      from its close, and since nothing here holds historical membership, such
      a window cannot be reconstructed — the honest answer is to refuse it, not
      to trade the name for whatever bars happen to be stored. Both window
      bounds are inclusive: a delisting on either edge still means the two ends
      disagree.

    A ledger row with no date cannot clear a window for a symbol that is in the
    dataset, so it raises rather than passing.

    Note that an **empty** ledger raises nothing: it simply cannot report an
    exit-side violation, because there is nothing in it to report. A caller
    relying on the exit half must therefore treat ``load_delistings() == {}`` as
    *unverified* and say so, not as *no delistings happened*.

    :param manifest: Universe manifest recording the PIT constituency.
    :param symbols_in_dataset: Symbols actually used in the research run.
    :param window_start, window_end: The run's window; both or neither.
    :param delistings: ``{symbol: delist_date}``, from :func:`load_delistings`.
    :raises PitViolation: on a symbol outside the manifest, or one delisted
        inside the window, or one whose delisting date is unknown.
    :raises ValueError: if a window is requested without the inputs needed to
        check it, rather than passing a window that was never examined.
    """
    manifest_set = set(manifest.symbols)
    extra = set(symbols_in_dataset) - manifest_set
    if extra:
        raise PitViolation(
            f"dataset contains symbols not in universe manifest "
            f"(as_of={manifest.as_of_date}): {sorted(extra)}"
        )

    if window_start is None and window_end is None and delistings is None:
        return

    if window_start is None or window_end is None or delistings is None:
        raise ValueError(
            "a point-in-time window check needs window_start, window_end and a "
            "delistings ledger together; a partial request would return a pass "
            "for a window that was never checked"
        )

    used = set(symbols_in_dataset)
    inside = sorted(
        (symbol, delist_date)
        for symbol, delist_date in delistings.items()
        if symbol in used
        and delist_date is not None
        and window_start <= delist_date <= window_end
    )
    undated = sorted(
        symbol
        for symbol, delist_date in delistings.items()
        if symbol in used and delist_date is None
    )
    if inside or undated:
        named = ", ".join(f"{symbol} delisted {d.isoformat()}" for symbol, d in inside)
        unknown = ", ".join(undated)
        raise PitViolation(
            f"universe is not point-in-time correct for window "
            f"[{window_start.isoformat()}, {window_end.isoformat()}] — membership "
            f"changed inside it and no historical index membership exists in this "
            f"repository to reconstruct the remainder. "
            + (f"Delisted inside the window: {named}. " if named else "")
            + (f"Delisted with no established date: {unknown}." if unknown else "")
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
    "load_delistings",
]
