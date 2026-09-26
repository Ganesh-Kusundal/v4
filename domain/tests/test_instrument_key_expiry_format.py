"""Compatibility guard for the instrument-key expiry wire format.

The expiry segment of an instrument key is a WIRE contract, not a display
format.  It is spelled once as ``INSTRUMENT_KEY_EXPIRY_FORMAT`` in
``tradex_domain.value_objects`` and consumed by ``InstrumentId.parse``,
``InstrumentId.__str__`` and ``wire.InstrumentRegistry._key``.

These tests pin the exact string value, prove ``InstrumentId`` round-trips
through the wire format unchanged, and prove keys produced by the old
hardcoded ``"%Y%m%d"`` literal still parse — the compatibility proof for
every already-stored provider key and every key an external client holds.

The dashed ``%Y-%m-%d`` form used by the CLI and the datalake is a DIFFERENT
contract and is deliberately not merged; see ``test_not_merged_with_dashed``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tradex_domain.value_objects import INSTRUMENT_KEY_EXPIRY_FORMAT, InstrumentId
from tradex_domain.wire import InstrumentRegistry

#: The literal that was hardcoded at all three sites before the constant
#: existed.  Kept here so the compatibility tests do not depend on the
#: constant when checking that stored keys still parse.
LEGACY_LITERAL = "%Y%m%d"

#: Year/month/DST-ish and leap boundaries where a wrong format would drift.
BOUNDARY_DATES = [
    date(2026, 1, 1),  # new year
    date(2026, 1, 30),  # month end, pre-DST
    date(2026, 3, 1),  # month end, pre-DST
    date(2026, 3, 29),  # month end, post-DST shift
    date(2026, 6, 30),  # half-year end
    date(2026, 12, 31),  # year end
    date(2027, 1, 1),  # year rollover
    date(2024, 2, 29),  # leap day
    date(2030, 11, 30),  # long horizon
]


class TestConstantValue:
    def test_value_is_the_legacy_literal(self):
        """The constant must be byte-identical to the old hardcoded format."""
        assert INSTRUMENT_KEY_EXPIRY_FORMAT == LEGACY_LITERAL
        assert INSTRUMENT_KEY_EXPIRY_FORMAT == "%Y%m%d"

    def test_not_merged_with_dashed(self):
        """The dashed CLI/datalake format is a different contract — stay separate."""
        assert INSTRUMENT_KEY_EXPIRY_FORMAT != "%Y-%m-%d"


class TestBoundaryFormatting:
    @pytest.mark.parametrize("d", BOUNDARY_DATES)
    def test_strftime_matches_legacy_literal(self, d):
        assert d.strftime(INSTRUMENT_KEY_EXPIRY_FORMAT) == d.strftime(LEGACY_LITERAL)

    @pytest.mark.parametrize("d", BOUNDARY_DATES)
    def test_instrument_id_expiry_segment_is_eight_digits(self, d):
        """Key shape is unchanged: 8 digits, no separators, no padding drift."""
        segment = str(InstrumentId.future("NSE", "NIFTY", d)).split(":")[2]
        assert segment == d.strftime(LEGACY_LITERAL)
        assert len(segment) == 8
        assert segment.isdigit()


class TestRoundTrip:
    @pytest.mark.parametrize("d", BOUNDARY_DATES)
    def test_future_round_trips_unchanged(self, d):
        original = InstrumentId.future("NSE", "NIFTY", d)
        assert InstrumentId.parse(str(original)) == original

    @pytest.mark.parametrize("d", BOUNDARY_DATES)
    def test_option_round_trips_unchanged(self, d):
        original = InstrumentId.option("NSE", "NIFTY", d, 20000, "CE")
        assert InstrumentId.parse(str(original)) == original

    def test_equity_without_expiry_round_trips(self):
        original = InstrumentId.equity("NSE", "RELIANCE")
        assert InstrumentId.parse(str(original)) == original
        assert str(original) == "NSE:RELIANCE"


class TestLegacyKeyCompatibility:
    """A key built with the OLD hardcoded literal must still parse unchanged."""

    @pytest.mark.parametrize("d", BOUNDARY_DATES)
    def test_parses_key_built_with_legacy_literal(self, d):
        legacy_key = f"NSE:NIFTY:{d.strftime(LEGACY_LITERAL)}:FUT"
        parsed = InstrumentId.parse(legacy_key)
        assert parsed.exchange == "NSE"
        assert parsed.underlying == "NIFTY"
        assert parsed.expiry == d
        assert parsed.right == "FUT"
        # And it re-serialises to exactly the same string.
        assert str(parsed) == legacy_key

    def test_parses_legacy_option_key(self):
        legacy_key = f"NSE:NIFTY:{date(2026, 1, 30).strftime(LEGACY_LITERAL)}:20000:PE"
        parsed = InstrumentId.parse(legacy_key)
        assert parsed.expiry == date(2026, 1, 30)
        assert parsed.strike == Decimal("20000")
        assert parsed.right == "PE"
        assert str(parsed) == "NSE:NIFTY:20260130:20000:PE"

    def test_wire_registry_key_unchanged(self):
        """Registry provider keys keep their exact historical spelling."""
        iid = InstrumentId.option("NSE", "NIFTY", date(2026, 1, 30), 20000, "CE")
        registry = InstrumentRegistry()
        registry.register(iid, {"asset_class": "OPTION"})
        assert registry.provider_key(iid) == "NSE_OPT|NIFTY:20260130:20000:CE"
        assert registry.resolve("NSE_OPT|NIFTY:20260130:20000:CE") == iid

    def test_wire_registry_future_key_unchanged(self):
        iid = InstrumentId.future("NSE", "NIFTY", date(2026, 12, 31))
        registry = InstrumentRegistry()
        registry.register(iid, {"asset_class": "FUTURE"})
        assert registry.provider_key(iid) == "NSE_FUT|NIFTY:20261231:FUT"

    def test_legacy_wire_key_still_resolves(self):
        """A key stored before the rename still resolves to the instrument."""
        iid = InstrumentId.future("NSE", "NIFTY", date(2026, 12, 31))
        registry = InstrumentRegistry()
        registry.register_authoritative(iid, "NSE_FUT|NIFTY:20261231:FUT")
        assert registry.resolve("NSE_FUT|NIFTY:20261231:FUT") == iid
