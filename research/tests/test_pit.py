"""Point-in-time (PIT) validator tests (TDD — written before implementation)."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from tradex_research.manifests import DatasetManifest, UniverseManifest
from tradex_research.pit import (
    PitViolation,
    assert_corporate_actions_timing,
    assert_feature_ts_leq_decision_ts,
    assert_historical_universe,
    assert_no_future_candle,
    assert_price_series_labelled,
)


# ---------------------------------------------------------------------------
# assert_no_future_candle / assert_feature_ts_leq_decision_ts
# ---------------------------------------------------------------------------


def _df(rows: list[tuple]) -> pd.DataFrame:
    """Build a minimal feature DataFrame from (feature_ts, decision_ts) pairs."""
    return pd.DataFrame(rows, columns=["feature_ts", "decision_ts"])


def test_no_future_candle_passes_when_feature_ts_leq_decision_ts() -> None:
    df = _df([
        (datetime(2026, 1, 2, 9, 15), datetime(2026, 1, 2, 9, 16)),
        (datetime(2026, 1, 2, 9, 16), datetime(2026, 1, 2, 9, 16)),  # equal — allowed
        (datetime(2026, 1, 2, 9, 17), datetime(2026, 1, 2, 9, 20)),
    ])
    assert_no_future_candle(df, feature_ts_col="feature_ts", decision_ts_col="decision_ts")


def test_no_future_candle_raises_on_lookahead() -> None:
    df = _df([
        (datetime(2026, 1, 2, 9, 15), datetime(2026, 1, 2, 9, 16)),
        (datetime(2026, 1, 2, 9, 18), datetime(2026, 1, 2, 9, 17)),  # future candle
    ])
    with pytest.raises(PitViolation, match="future candle"):
        assert_no_future_candle(df, feature_ts_col="feature_ts", decision_ts_col="decision_ts")


def test_no_future_candle_names_offending_timestamps() -> None:
    bad_ft = datetime(2026, 3, 15, 14, 0)
    bad_dt = datetime(2026, 3, 15, 13, 55)
    df = _df([(bad_ft, bad_dt)])
    with pytest.raises(PitViolation) as exc_info:
        assert_no_future_candle(df, feature_ts_col="feature_ts", decision_ts_col="decision_ts")
    msg = str(exc_info.value)
    assert "2026-03-15" in msg


def test_feature_ts_leq_decision_ts_is_alias() -> None:
    """assert_feature_ts_leq_decision_ts is the same guard under a pipeline-friendly name."""
    df = _df([(datetime(2026, 1, 2, 9, 20), datetime(2026, 1, 2, 9, 19))])
    with pytest.raises(PitViolation):
        assert_feature_ts_leq_decision_ts(
            df, feature_ts_col="feature_ts", decision_ts_col="decision_ts"
        )


def test_no_future_candle_empty_dataframe_passes() -> None:
    df = pd.DataFrame(columns=["feature_ts", "decision_ts"])
    assert_no_future_candle(df, feature_ts_col="feature_ts", decision_ts_col="decision_ts")


# ---------------------------------------------------------------------------
# assert_historical_universe
# ---------------------------------------------------------------------------


def _universe(symbols: tuple[str, ...], as_of: date = date(2026, 1, 1)) -> UniverseManifest:
    return UniverseManifest(
        universe_name="nifty50",
        as_of_date=as_of,
        symbols=symbols,
    )


def test_historical_universe_passes_when_all_symbols_in_manifest() -> None:
    manifest = _universe(("RELIANCE", "TCS", "INFY"))
    assert_historical_universe(manifest, ["RELIANCE", "TCS"])


def test_historical_universe_passes_with_full_match() -> None:
    manifest = _universe(("RELIANCE", "TCS"))
    assert_historical_universe(manifest, ["TCS", "RELIANCE"])


def test_historical_universe_raises_on_extra_symbol() -> None:
    manifest = _universe(("RELIANCE", "TCS"))
    with pytest.raises(PitViolation, match="NEWCO"):
        assert_historical_universe(manifest, ["RELIANCE", "TCS", "NEWCO"])


def test_historical_universe_violation_message_contains_as_of_date() -> None:
    manifest = _universe(("RELIANCE",), as_of=date(2026, 6, 1))
    with pytest.raises(PitViolation) as exc_info:
        assert_historical_universe(manifest, ["RELIANCE", "LATECOMER"])
    assert "2026-06-01" in str(exc_info.value)


# ---------------------------------------------------------------------------
# assert_corporate_actions_timing
# ---------------------------------------------------------------------------


def test_ca_timing_passes_when_all_ex_dates_leq_reference() -> None:
    actions = [
        {"ex_date": date(2026, 1, 10), "action_type": "SPLIT"},
        {"ex_date": date(2026, 2, 14), "action_type": "DIVIDEND"},
    ]
    assert_corporate_actions_timing("RELIANCE", actions, reference_date=date(2026, 3, 1))


def test_ca_timing_passes_when_ex_date_equals_reference() -> None:
    actions = [{"ex_date": date(2026, 3, 1), "action_type": "BONUS"}]
    assert_corporate_actions_timing("RELIANCE", actions, reference_date=date(2026, 3, 1))


def test_ca_timing_raises_when_ex_date_after_reference() -> None:
    actions = [{"ex_date": date(2026, 4, 1), "action_type": "SPLIT"}]
    with pytest.raises(PitViolation, match="PIT violation"):
        assert_corporate_actions_timing("RELIANCE", actions, reference_date=date(2026, 3, 1))


def test_ca_timing_works_with_iso_string_dates() -> None:
    actions = [{"ex_date": "2026-05-15", "action_type": "DIVIDEND"}]
    # reference before ex_date → violation
    with pytest.raises(PitViolation):
        assert_corporate_actions_timing("TCS", actions, reference_date=date(2026, 4, 1))
    # reference on/after ex_date → ok
    assert_corporate_actions_timing("TCS", actions, reference_date=date(2026, 5, 15))


def test_ca_timing_names_symbol_in_violation() -> None:
    actions = [{"ex_date": date(2026, 12, 1), "action_type": "SPLIT"}]
    with pytest.raises(PitViolation) as exc_info:
        assert_corporate_actions_timing("HDFC", actions, reference_date=date(2026, 6, 1))
    assert "HDFC" in str(exc_info.value)


def test_ca_timing_empty_actions_passes() -> None:
    assert_corporate_actions_timing("INFY", [], reference_date=date(2026, 1, 1))


# ---------------------------------------------------------------------------
# assert_price_series_labelled
# ---------------------------------------------------------------------------


def _ds(price_series: str) -> DatasetManifest:
    # bypass the constructor validation to test the guard function independently
    # by using a valid value that we'll test the guard is explicit about
    return DatasetManifest(
        symbols=("RELIANCE",),
        timeframe="1m",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 3, 31),
        data_hash="abc",
        price_series=price_series,
    )


def test_price_series_labelled_passes_for_raw() -> None:
    assert_price_series_labelled(_ds("raw"))


def test_price_series_labelled_passes_for_adjusted() -> None:
    assert_price_series_labelled(_ds("adjusted"))


def test_price_series_labelled_is_explicit_guard_callable() -> None:
    """The guard is callable and importable — pipeline checkpoints can call it."""
    import inspect
    from tradex_research.pit import assert_price_series_labelled as guard
    assert callable(guard)
    sig = inspect.signature(guard)
    assert "manifest" in sig.parameters
