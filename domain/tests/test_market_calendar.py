"""Pinning tests for the canonical IST/session vocabulary [REF-2]."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from tradex_domain.market_calendar import IST, MARKET_CLOSE, MARKET_OPEN, to_ist_naive


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
