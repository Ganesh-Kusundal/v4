"""Tests for InstrumentId.parse()."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tradex_domain.value_objects import InstrumentId


class TestParseEquity:
    def test_nse_reliance(self):
        iid = InstrumentId.parse("NSE:RELIANCE")
        assert iid.exchange == "NSE"
        assert iid.underlying == "RELIANCE"
        assert iid.expiry is None
        assert iid.strike is None
        assert iid.right is None

    def test_bse_equity(self):
        iid = InstrumentId.parse("BSE:INFY")
        assert iid.exchange == "BSE"
        assert iid.underlying == "INFY"


class TestParseFuture:
    def test_nifty_future(self):
        iid = InstrumentId.parse("NSE:NIFTY:20260130:FUT")
        assert iid.exchange == "NSE"
        assert iid.underlying == "NIFTY"
        assert iid.expiry == date(2026, 1, 30)
        assert iid.right == "FUT"
        assert iid.strike is None


class TestParseOption:
    def test_nifty_call(self):
        iid = InstrumentId.parse("NSE:NIFTY:20260130:20000:CE")
        assert iid.exchange == "NSE"
        assert iid.underlying == "NIFTY"
        assert iid.expiry == date(2026, 1, 30)
        assert iid.strike == Decimal("20000")
        assert iid.right == "CE"

    def test_nifty_put(self):
        iid = InstrumentId.parse("NSE:NIFTY:20260130:19000:PE")
        assert iid.right == "PE"
        assert iid.strike == Decimal("19000")


class TestParseInvalid:
    def test_single_segment_raises(self):
        with pytest.raises(ValueError):
            InstrumentId.parse("NSE")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            InstrumentId.parse("")

    def test_invalid_exchange_raises(self):
        with pytest.raises(ValueError):
            InstrumentId.parse("INVALID:RELIANCE")

    def test_invalid_date_raises(self):
        with pytest.raises(ValueError):
            InstrumentId.parse("NSE:NIFTY:NOTADATE:FUT")

    def test_invalid_right_raises(self):
        with pytest.raises(ValueError):
            InstrumentId.parse("NSE:NIFTY:20260130:20000:INVALID")


class TestStr:
    def test_equity_str(self):
        iid = InstrumentId.parse("NSE:RELIANCE")
        assert str(iid) == "NSE:RELIANCE"

    def test_future_str(self):
        iid = InstrumentId.parse("NSE:NIFTY:20260130:FUT")
        assert str(iid) == "NSE:NIFTY:20260130:FUT"

    def test_option_str(self):
        iid = InstrumentId.parse("NSE:NIFTY:20260130:20000:CE")
        assert str(iid) == "NSE:NIFTY:20260130:20000:CE"
