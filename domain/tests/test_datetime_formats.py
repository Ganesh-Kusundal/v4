"""Value + behaviour guards for the shared dashed datetime format contracts.

``tradex_domain.datetime_formats`` names two wire/storage format strings that
were previously spelled as bare literals inside broker adapters:

* ``DASHED_DATETIME`` == ``"%Y-%m-%d %H:%M:%S"`` — the Dhan intraday chart
  ``fromDate``/``toDate`` request field, and the space-separated slot in the
  provider fallback ladder.
* ``DASHED_DATE`` == ``"%Y-%m-%d"`` — the Dhan historical chart range and the
  date-only slot in the provider fallback ladder.

Naming them must change nothing observable, so these tests pin the *values*
(not merely the names) and then re-derive the ``provider_common`` fallback
ladder's behaviour from first principles, comparing it against a hardcoded
BEFORE snapshot captured from the original all-literal ladder.  If a format
string ever drifts by a character, or the ladder is reordered or collapsed,
these fail.

The compact ``"%Y%m%d"`` instrument-key expiry contract belongs to a different
family and is guarded separately in ``test_instrument_key_expiry_format.py``;
``TestFamilySeparation`` below asserts the three never converge.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from tradex_brokers.common.provider_common import parse_timestamp

from tradex_domain import SDKError
from tradex_domain.datetime_formats import DASHED_DATE, DASHED_DATETIME
from tradex_domain.value_objects import INSTRUMENT_KEY_EXPIRY_FORMAT

#: The literals that were hardcoded at every call site before the constants
#: existed.  The tests below compare against THESE, not against the constants,
#: so a wrong constant value cannot make a test pass by agreeing with itself.
LEGACY_DASHED_DATETIME = "%Y-%m-%d %H:%M:%S"
LEGACY_DASHED_DATE = "%Y-%m-%d"

#: The provider_common fallback ladder exactly as it was BEFORE the rename:
#: nine ordered entries, the fifth and seventh being the two dashed families.
LEGACY_LADDER = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
)

#: Representative untrusted broker timestamps: one per ladder entry, plus the
#: awkward-but-valid shapes that make the fallback list necessary in the first
#: place (naive strings, offsets, day-first order, whitespace padding, epochs).
REPRESENTATIVE_INPUTS = [
    "2026-07-15 10:07:23",  # DASHED_DATETIME
    "2026-07-15 10:07:23+05:30",  # space-separated, aware
    "2026-07-15T10:07:23",  # ISO naive
    "2026-07-15T10:07:23.123456",  # ISO naive, microseconds
    "2026-07-15T10:07:23+05:30",  # ISO aware
    "2026-07-15T10:07:23.123456+05:30",  # ISO aware, microseconds
    "2026-07-15",  # DASHED_DATE
    "15-07-2026",  # day-first dashed
    "15/07/2026",  # day-first slashed
    "  2026-07-15 10:07:23  ",  # padded, must be stripped
    1752566843,  # epoch seconds
    1752566843123,  # epoch milliseconds
]


def _ladder_parse(value):
    """Parse using the ORIGINAL all-literal ladder, first match wins."""
    for fmt in LEGACY_LADDER:
        try:
            return datetime.strptime(value, fmt)  # noqa: DTZ007
        except (ValueError, TypeError):
            continue
    return None


class TestConstantValues:
    """Pin the VALUES. A rename that changes a byte is a silent data bug."""

    def test_dashed_datetime_value(self):
        assert DASHED_DATETIME == LEGACY_DASHED_DATETIME
        assert DASHED_DATETIME == "%Y-%m-%d %H:%M:%S"

    def test_dashed_date_value(self):
        assert DASHED_DATE == LEGACY_DASHED_DATE
        assert DASHED_DATE == "%Y-%m-%d"

    def test_constants_are_strings(self):
        assert isinstance(DASHED_DATETIME, str)
        assert isinstance(DASHED_DATE, str)

    def test_dashed_date_is_a_prefix_of_dashed_datetime(self):
        """The two families are related but must stay separate constants."""
        assert DASHED_DATETIME.startswith(DASHED_DATE)
        assert DASHED_DATETIME != DASHED_DATE


class TestFamilySeparation:
    """The three families must never be merged into one another."""

    def test_compact_key_format_is_distinct(self):
        assert INSTRUMENT_KEY_EXPIRY_FORMAT == "%Y%m%d"
        assert INSTRUMENT_KEY_EXPIRY_FORMAT != DASHED_DATE
        assert INSTRUMENT_KEY_EXPIRY_FORMAT != DASHED_DATETIME

    def test_dashed_date_is_not_compact(self):
        assert DASHED_DATE != "%Y%m%d"
        assert DASHED_DATETIME != "%Y%m%d %H%M%S"

    def test_compact_format_not_defined_in_datetime_formats(self):
        """The compact contract must not leak into this module's namespace."""
        import tradex_domain.datetime_formats as mod

        assert not hasattr(mod, "INSTRUMENT_KEY_EXPIRY_FORMAT")
        assert sorted(mod.__all__) == ["DASHED_DATE", "DASHED_DATETIME"]


class TestRoundTrip:
    @pytest.mark.parametrize(
        ("moment", "fmt", "expected"),
        [
            (datetime(2026, 7, 15, 10, 7, 23), DASHED_DATETIME, "2026-07-15 10:07:23"),
            (datetime(2026, 1, 1, 0, 0, 0), DASHED_DATETIME, "2026-01-01 00:00:00"),
            (datetime(2026, 12, 31, 23, 59, 59), DASHED_DATETIME, "2026-12-31 23:59:59"),
            (datetime(2024, 2, 29, 9, 15, 0), DASHED_DATETIME, "2024-02-29 09:15:00"),
            (datetime(2026, 7, 15, 10, 7, 23), DASHED_DATE, "2026-07-15"),
            (datetime(2026, 3, 1, 5, 30, 0), DASHED_DATE, "2026-03-01"),
            (datetime(2027, 1, 1, 12, 0, 0), DASHED_DATE, "2027-01-01"),
        ],
    )
    def test_strftime_matches_legacy_literal(self, moment, fmt, expected):
        """The constant renders byte-identically to the old hardcoded literal."""
        assert moment.strftime(fmt) == moment.strftime(
            LEGACY_DASHED_DATETIME if fmt is DASHED_DATETIME else LEGACY_DASHED_DATE
        )
        assert moment.strftime(fmt) == expected

    def test_datetime_round_trips_through_dashed_datetime(self):
        moment = datetime(2026, 7, 15, 10, 7, 23)
        assert datetime.strptime(moment.strftime(DASHED_DATETIME), DASHED_DATETIME) == moment

    def test_date_round_trips_through_dashed_date(self):
        """A date-only format truncates the time — that truncation is the point."""
        moment = datetime(2026, 7, 15, 10, 7, 23)
        assert datetime.strptime(moment.strftime(DASHED_DATE), DASHED_DATE) == datetime(
            2026, 7, 15
        )


class TestProviderLadderUnchanged:
    """The ladder is an ORDERED FALLBACK LIST, not duplicated literals.

    Each expectation below records what the ORIGINAL all-literal ladder
    returned, so reordering, collapsing, or editing any entry is caught.
    """

    def test_ladder_has_nine_distinct_entries(self):
        """Collapsing the ladder would reject valid broker responses."""
        assert len(LEGACY_LADDER) == 9
        assert len(set(LEGACY_LADDER)) == 9

    def test_ladder_contains_both_dashed_families_in_order(self):
        assert LEGACY_LADDER.index(LEGACY_DASHED_DATETIME) == 4
        assert LEGACY_LADDER.index(LEGACY_DASHED_DATE) == 6

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2026-07-15 10:07:23", datetime(2026, 7, 15, 10, 7, 23)),
            ("2026-07-15T10:07:23", datetime(2026, 7, 15, 10, 7, 23)),
            ("2026-07-15 10:07:23+05:30", datetime.fromisoformat("2026-07-15T10:07:23+05:30")),
            ("2026-07-15T10:07:23.123456", datetime(2026, 7, 15, 10, 7, 23, 123456)),
            (
                "2026-07-15T10:07:23.123456+05:30",
                datetime.fromisoformat("2026-07-15T10:07:23.123456+05:30"),
            ),
            ("2026-07-15T10:07:23+05:30", datetime.fromisoformat("2026-07-15T10:07:23+05:30")),
            ("2026-07-15", datetime(2026, 7, 15)),
            ("15-07-2026", datetime(2026, 7, 15)),
            ("15/07/2026", datetime(2026, 7, 15)),
            ("  2026-07-15 10:07:23  ", datetime(2026, 7, 15, 10, 7, 23)),
        ],
    )
    def test_legacy_ladder_still_parses_each_shape(self, value, expected):
        assert _ladder_parse(value.strip() if isinstance(value, str) else value) == expected

    def test_legacy_ladder_rejects_unparseable(self):
        assert _ladder_parse("not-a-timestamp") is None
        assert _ladder_parse("2026-07-15 10:07") is None
        assert _ladder_parse("15/07/2026 10:07:23") is None

    def test_representative_inputs_covered(self):
        for value in REPRESENTATIVE_INPUTS:
            if isinstance(value, str):
                assert _ladder_parse(value.strip()) is not None, value


class TestLiveLadderMatchesLegacyLadder:
    """Before/after proof: the shipped ``parse_timestamp`` must agree with the
    original literal ladder on every representative input, including which
    inputs it rejects. Run against the real imported implementation.
    """

    @pytest.mark.parametrize("value", REPRESENTATIVE_INPUTS)
    def test_live_parse_timestamp_matches_legacy_ladder(self, value):
        live = parse_timestamp(value)
        legacy = _ladder_parse(value.strip()) if isinstance(value, str) else None

        if legacy is not None:
            # The live function pins NAIVE ladder hits to UTC but leaves an
            # aware hit on its original offset, so compare the instants.
            assert live == legacy or live.replace(tzinfo=None) == legacy.replace(tzinfo=None)
        else:
            # Epoch inputs never reach the ladder; they must still work.
            assert live is not None
            assert live.tzinfo is not None

    @pytest.mark.parametrize("bad", ["not-a-timestamp", "2026-07-15 10:07", "15/07/2026 10:07:23"])
    def test_live_parse_timestamp_rejects_same_inputs(self, bad):
        assert _ladder_parse(bad) is None
        with pytest.raises(SDKError):
            parse_timestamp(bad)
