"""Pinning tests for the canonical IST/session vocabulary [REF-2]."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from tradex_domain.market_calendar import (
    IST,
    MARKET_CLOSE,
    MARKET_OPEN,
    NSE_HOLIDAYS_2026,
    to_ist_naive,
)


def test_ist_offset() -> None:
    assert IST.utcoffset(datetime(2026, 1, 1, tzinfo=UTC)) == timedelta(hours=5, minutes=30)


def test_to_ist_naive_converts_utc() -> None:
    utc_noon = datetime(2026, 8, 22, 6, 30, tzinfo=UTC)  # 12:00 IST
    assert to_ist_naive(utc_noon) == datetime(2026, 8, 22, 12, 0)


def test_to_ist_naive_passthrough_naive() -> None:
    naive = datetime(2026, 8, 22, 12, 0)
    assert to_ist_naive(naive) == naive


def test_market_hours_pinned() -> None:
    assert (MARKET_OPEN, MARKET_CLOSE) == (time(9, 15), time(15, 30))


def test_normalize_symbol_flag() -> None:
    from tradex_domain.value_objects import normalize_symbol

    # public default preserves wire behavior (strip suffixes)
    assert normalize_symbol(" reliance-eq ") == "RELIANCE"
    # InstrumentId path opts out: raw underlying preserved
    assert normalize_symbol("reliance-eq", strip_provider_suffixes=False) == "RELIANCE-EQ"


def test_nse_holidays_2026_are_dates() -> None:
    # Every entry must be a weekday (weekend closures are unreachable here).
    for d in NSE_HOLIDAYS_2026:
        assert isinstance(d, date)
        assert d.weekday() < 5


def test_nse_holidays_2026_seeded_from_store_evidence() -> None:
    # Empirically confirmed market-wide closures: zero bars across every
    # tracked symbol even after a clean Dhan backfill.
    assert date(2026, 5, 28) in NSE_HOLIDAYS_2026
    assert date(2026, 6, 26) in NSE_HOLIDAYS_2026
