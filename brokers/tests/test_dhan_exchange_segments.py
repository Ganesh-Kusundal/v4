"""The Dhan exchange-segment tables must never disagree (order-routing).

The forward table (domain exchange -> Dhan ``ExchangeSegment``) and the binary
wire-code table (frame-header int -> domain exchange) used to be maintained
independently. They now share one source of truth in
``tradex_brokers.common.dhan_segments`` with an import-time sync assertion, and
these tests pin the values so a future SDK bump cannot move a segment silently.

``dhan/instruments.py:SEGMENT_CANONICAL`` is a *different* key space — master
CSV ``(SEM_EXM_EXCH_ID, SEM_SEGMENT)`` pairs — and is asserted untouched here.
"""

from __future__ import annotations

import pytest

from tradex_brokers.common import dhan_segments
from tradex_brokers.common.dhan_segments import (
    DEFAULT_DHAN_EXCHANGE,
    DEFAULT_DHAN_SEGMENT,
    DHAN_EXCHANGE_SEGMENT,
    DHAN_SEGMENT_EXCHANGE,
    DHAN_WIRE_CODE_EXCHANGE,
    dhan_segment_for,
)
from tradex_brokers.dhan.client import _DHAN_EXCHANGE_SEGMENT, dhan_exchange_segment
from tradex_brokers.dhan.instruments import SEGMENT_CANONICAL
from tradex_brokers.dhan.tick_parser import SEGMENT_EXCHANGE, _assert_wire_codes_match_segments

#: Canonical forward table pinned to the ``dhanhq`` SDK exchange constants.
#: NSE/BSE/NFO/BFO/MCX/CDS/IDX are ``dhanhq.NSE``/``BSE``/``FNO``/``BSE_FNO``/
#: ``MCX``/``CUR``/``INDEX``; NSE_COMM and BCD have no standalone SDK constant
#: and are pinned to Dhan's documented per-exchange segment strings.
SDK_EXPECTED_SEGMENTS: dict[str, str] = {
    "NSE": "NSE_EQ",
    "BSE": "BSE_EQ",
    "NFO": "NSE_FNO",
    "BFO": "BSE_FNO",
    "MCX": "MCX_COMM",
    "CDS": "NSE_CURRENCY",
    "IDX": "IDX_I",
    "NSE_COMM": "NSE_COMM",
    "BCD": "BSE_CURRENCY",
}

#: The binary protocol codes, pinned.  Dhan leaves 6 unassigned and never sends
#: a code for NSE_COMM — a deliberate gap, not an oversight.
WIRE_CODES: dict[int, str] = {
    0: "IDX",
    1: "NSE",
    2: "NFO",
    3: "CDS",
    4: "BSE",
    5: "MCX",
    7: "BCD",
    8: "BFO",
}


# ---------------------------------------------------------------------------
# Forward table
# ---------------------------------------------------------------------------


class TestForwardTable:
    def test_every_mapping_equals_the_sdk_constant(self):
        """All nine entries, pinned — the table IS the SDK's values."""
        assert DHAN_EXCHANGE_SEGMENT == SDK_EXPECTED_SEGMENTS

    @pytest.mark.parametrize(("exchange", "segment"), sorted(SDK_EXPECTED_SEGMENTS.items()))
    def test_individual_entry(self, exchange: str, segment: str) -> None:
        assert DHAN_EXCHANGE_SEGMENT[exchange] == segment

    def test_client_reexport_is_the_same_table(self):
        """``client._DHAN_EXCHANGE_SEGMENT`` stays a working backwards-compatible name."""
        assert _DHAN_EXCHANGE_SEGMENT == DHAN_EXCHANGE_SEGMENT

    def test_reverse_table_is_the_exact_inverse(self):
        assert DHAN_SEGMENT_EXCHANGE == {
            "NSE_EQ": "NSE",
            "BSE_EQ": "BSE",
            "NSE_FNO": "NFO",
            "BSE_FNO": "BFO",
            "MCX_COMM": "MCX",
            "NSE_COMM": "NSE_COMM",
            "NSE_CURRENCY": "CDS",
            "BSE_CURRENCY": "BCD",
            "IDX_I": "IDX",
        }

    def test_default_is_nse_eq(self):
        assert DEFAULT_DHAN_EXCHANGE == "NSE"
        assert DEFAULT_DHAN_SEGMENT == "NSE_EQ"


# ---------------------------------------------------------------------------
# Wire-code table
# ---------------------------------------------------------------------------


class TestWireCodeTable:
    def test_wire_codes_pinned(self):
        assert SEGMENT_EXCHANGE == WIRE_CODES

    def test_wire_codes_round_trip_through_the_forward_table(self):
        """``forward[forward_inv[wire[code]]] == wire[code]`` for every code."""
        for code, exchange in SEGMENT_EXCHANGE.items():
            segment = DHAN_EXCHANGE_SEGMENT[exchange]
            assert DHAN_SEGMENT_EXCHANGE[segment] == exchange, (
                f"wire code {code} -> {exchange} does not round-trip"
            )

    def test_no_conflicting_entries(self):
        """Every wire exchange is known, and no two codes claim one exchange."""
        assert not set(SEGMENT_EXCHANGE.values()) - set(DHAN_EXCHANGE_SEGMENT)
        assert len(set(SEGMENT_EXCHANGE.values())) == len(SEGMENT_EXCHANGE)

    def test_code_six_is_still_unassigned(self):
        """Dhan assigns no segment to wire code 6; we must not invent one."""
        assert SEGMENT_EXCHANGE.get(6) is None
        with pytest.raises(KeyError):
            SEGMENT_EXCHANGE[6]

    def test_tick_parser_reexports_the_pinned_list(self):
        """``SEGMENT_EXCHANGE`` is a copy of the one pinned code list."""
        assert SEGMENT_EXCHANGE == DHAN_WIRE_CODE_EXCHANGE

    def test_sync_assert_accepts_the_pinned_list(self):
        assert _assert_wire_codes_match_segments() == DHAN_WIRE_CODE_EXCHANGE

    def test_sync_assert_rejects_a_wire_code_for_an_unknown_exchange(self, monkeypatch):
        """A code naming an exchange absent from the forward table is fatal."""
        monkeypatch.setitem(
            dhan_segments.DHAN_WIRE_CODE_EXCHANGE, 9, "BSE_EQ"
        )
        with pytest.raises(AssertionError, match="missing from the canonical"):
            _assert_wire_codes_match_segments()

    def test_sync_assert_catches_a_forward_side_repoint(self, monkeypatch):
        """The real drift case: a forward entry re-pointed under the wire list.

        CDS -> BSE_CURRENCY leaves wire code 3 still saying "CDS", but it now
        resolves to BCD, i.e. market data routed to the wrong exchange. The
        import-time assert fails instead of shipping it.
        """
        monkeypatch.setitem(dhan_segments.DHAN_EXCHANGE_SEGMENT, "CDS", "BSE_CURRENCY")
        with pytest.raises(AssertionError, match="disagree with the canonical"):
            _assert_wire_codes_match_segments()

    def test_sync_assert_catches_a_dropped_forward_entry(self, monkeypatch):
        """Deleting a forward row must fail too, not silently drop a code."""
        monkeypatch.delitem(dhan_segments.DHAN_EXCHANGE_SEGMENT, "MCX")
        with pytest.raises(AssertionError, match="missing from the canonical"):
            _assert_wire_codes_match_segments()

    def test_wire_codes_are_not_positional_offsets_into_the_forward_table(self):
        """Dhan's code list skips 6, so it is not ``enumerate()`` of the rows.

        This is the property that would break if someone "derived" the codes:
        positional row indices would send code 6 to BCD and shift every later
        code. A guard belongs here rather than in the import assert, because
        this invariant is what we actually want to keep, not a coding style.
        """
        positional = {
            code: exchange for code, exchange in enumerate(DHAN_EXCHANGE_SEGMENT)
        }
        assert positional != DHAN_WIRE_CODE_EXCHANGE
        assert positional[6] == "BCD"  # what a derived table would wrongly send
        assert 6 not in DHAN_WIRE_CODE_EXCHANGE
        assert max(DHAN_WIRE_CODE_EXCHANGE) == 8

    def test_nse_comm_has_no_wire_code(self):
        """NSE_COMM is a REST segment with no binary code — the gap is expected."""
        assert "NSE_COMM" in DHAN_EXCHANGE_SEGMENT
        assert "NSE_COMM" not in set(SEGMENT_EXCHANGE.values())

# ---------------------------------------------------------------------------
# Unknown-input behaviour must be unchanged
# ---------------------------------------------------------------------------


class TestUnknownInput:
    @pytest.mark.parametrize("value", ["ZZZ", "", "  ", None, 42, "NOT_A_SEGMENT"])
    def test_forward_lookup_falls_back_to_nse_eq(self, value: object) -> None:
        assert dhan_exchange_segment(value) == "NSE_EQ"
        assert dhan_segment_for(value) == "NSE_EQ"

    @pytest.mark.parametrize("value", ["nse", " nse ", "bse", "nfo"])
    def test_case_and_whitespace_are_normalised(self, value: str) -> None:
        assert dhan_exchange_segment(value) == DHAN_EXCHANGE_SEGMENT[
            value.strip().upper()
        ]

    @pytest.mark.parametrize("code", [6, 9, 99, -1, 0xFFFF])
    def test_wire_lookup_returns_none_for_unknown_code(self, code: int) -> None:
        """``.get()`` with no default — ws_streams relies on the ``None``."""
        assert SEGMENT_EXCHANGE.get(code) is None

    def test_enum_input_uses_its_value(self) -> None:
        """Domain exchange enums resolve through ``.value``, as before."""

        class Fake:
            value = "bfo"

        assert dhan_exchange_segment(Fake()) == "BSE_FNO"


# ---------------------------------------------------------------------------
# SEGMENT_CANONICAL is a different key space and must stay untouched
# ---------------------------------------------------------------------------


class TestMasterSegmentCanonicalUntouched:
    def test_key_space_is_csv_pairs_not_rest_segments(self) -> None:
        assert SEGMENT_CANONICAL[("NSE", "E")] == "NSE"
        assert SEGMENT_CANONICAL[("NSE", "D")] == "NFO"
        assert SEGMENT_CANONICAL[("MCX", "M")] == "MCX"
        # Keys are CSV pairs; the values are bare domain exchanges and never
        # Dhan "NSE_EQ"-style segment strings.
        assert all(isinstance(k, tuple) and len(k) == 2 for k in SEGMENT_CANONICAL)
        # ...whereas the forward table's values are exactly those segment strings.
        assert set(DHAN_EXCHANGE_SEGMENT.values()) & {"NSE_EQ", "NSE_FNO", "IDX_I"}

    def test_not_merged_into_the_canonical_table(self) -> None:
        # NSE_COMM lives in the REST table but has no bare-exchange entry here;
        # the CSV pair ("NSE", "M") is the master row's own spelling of it.
        assert "NSE_COMM" not in SEGMENT_CANONICAL
        assert SEGMENT_CANONICAL[("NSE", "M")] == "NSE_COMM"
        assert DHAN_EXCHANGE_SEGMENT["NSE_COMM"] == "NSE_COMM"
