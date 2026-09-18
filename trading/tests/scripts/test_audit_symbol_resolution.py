"""Tests for ``audit_symbol_resolution.py`` — the audit must be able to fail.

The audit answers two questions, and both have a failure mode that is invisible
without a test:

1. **Resolution.** The synthetic masters below are the real CHOLAFIN collision
   shape — an equity and a debenture sharing a trading symbol — in both row
   orders. An audit that passes every master is worthless, so the negative case
   asserts it *catches* a master whose equity row loses its key.
2. **Classification.** ``_verdict`` is what turns the source cross-check into a
   finding, distinguishing a bad print (the lake inherited one broker's value)
   from a re-based series (the lake sits at a different level than both).

Everything here is offline: the audit's default path reads cached masters, and
these tests hand it synthetic bytes instead.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent
    / "scripts"
    / "audit_symbol_resolution.py"
)
_spec = importlib.util.spec_from_file_location("audit_symbol_resolution", _SCRIPT)
assert _spec is not None and _spec.loader is not None, _SCRIPT
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)

_agrees = audit._agrees
_verdict = audit._verdict
_upstox_resolution = audit._upstox_resolution
_dhan_resolution = audit._dhan_resolution

_EQUITY_ISIN = "INE121A01024"
_DEBENTURE_ISIN = "INE121A08PJ0"


def _upstox_row(key: str, isin: str, series: str) -> dict[str, object]:
    """One raw Upstox master row (the fields the loader and the audit read)."""
    return {
        "segment": "NSE_EQ", "exchange": "NSE", "isin": isin,
        "instrument_key": key, "trading_symbol": "CHOLAFIN",
        "instrument_type": series, "name": "CHOLAMANDALAM IN & FIN CO",
    }


def _upstox_master(*rows: dict[str, object]) -> bytes:
    return json.dumps(list(rows)).encode("utf-8")


@pytest.mark.parametrize("debenture_last", [True, False], ids=["equity-first", "debenture-first"])
def test_upstox_audit_is_silent_on_the_correct_master(debenture_last: bool) -> None:
    """The production loader now wins the equity key in either row order, so
    the audit finds nothing to report whichever way the vendor prints them."""
    rows = [_upstox_row("NSE_EQ|" + _EQUITY_ISIN, _EQUITY_ISIN, "EQ"),
            _upstox_row("NSE_EQ|" + _DEBENTURE_ISIN, _DEBENTURE_ISIN, "D1")]
    if not debenture_last:
        rows.reverse()
    report = _upstox_resolution(_upstox_master(*rows), {"CHOLAFIN": _EQUITY_ISIN})
    assert report["unresolved"] == []
    assert report["wrong_isin"] == []
    assert len(report["contested"]) == 1, "the collision itself must still be reported"


def test_upstox_audit_catches_a_debenture_owning_the_equity_key() -> None:
    """The negative case: with the claim priority disabled the debenture wins
    the key and the audit must say so — this is the CHOLAFIN bug, and an audit
    that cannot report it is not an audit."""
    from tradex_brokers.upstox.adapter import UpstoxBroker

    original = UpstoxBroker._row_claim_priority
    UpstoxBroker._row_claim_priority = lambda self, row: 0
    try:
        raw = _upstox_master(
            _upstox_row("NSE_EQ|" + _EQUITY_ISIN, _EQUITY_ISIN, "EQ"),
            _upstox_row("NSE_EQ|" + _DEBENTURE_ISIN, _DEBENTURE_ISIN, "D1"),
        )
        report = _upstox_resolution(raw, {"CHOLAFIN": _EQUITY_ISIN})
    finally:
        UpstoxBroker._row_claim_priority = original
    assert len(report["wrong_isin"]) == 1
    assert report["wrong_isin"][0]["symbol"] == "CHOLAFIN"
    assert report["wrong_isin"][0]["key_isin"] == _DEBENTURE_ISIN


def test_upstox_audit_reports_an_unresolved_symbol() -> None:
    report = _upstox_resolution(_upstox_master(), {"CHOLAFIN": _EQUITY_ISIN})
    assert report["unresolved"] == ["CHOLAFIN"]


def _dhan_csv(*rows: tuple[str, str, str, str]) -> bytes:
    """Minimal Dhan scrip-master CSV: (security_id, symbol, series, instrument)."""
    lines = ["SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_TRADING_SYMBOL,"
             "SEM_SERIES,SEM_INSTRUMENT_NAME"]
    lines += [f"NSE,E,{sid},{sym},{series},{name}" for sid, sym, series, name in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_dhan_audit_uses_the_series_because_its_master_has_no_isin() -> None:
    """Dhan publishes no ISIN, so the equity must be evidenced by series EQ."""
    raw = _dhan_csv(("19257", "CHOLAFIN", "D1", "EQUITY"),
                    ("685", "CHOLAFIN", "EQ", "EQUITY"))
    report = _dhan_resolution(raw, {"CHOLAFIN": _EQUITY_ISIN})
    assert report["unresolved"] == []
    assert report["wrong_series"] == []
    assert len(report["contested"]) == 1


def test_dhan_audit_flags_a_symbol_stuck_on_a_debt_series() -> None:
    """A symbol whose only row is a debenture is a wrong-security resolution."""
    raw = _dhan_csv(("19257", "CHOLAFIN", "D1", "EQUITY"))
    report = _dhan_resolution(raw, {"CHOLAFIN": _EQUITY_ISIN})
    assert [item["symbol"] for item in report["wrong_series"]] == ["CHOLAFIN"]
    assert report["wrong_series"][0]["series"] == "D1"


class TestVerdict:
    """``_verdict`` decides whether a jump is a finding, so pin its three cases."""

    def test_silent_when_everyone_agrees(self) -> None:
        assert _verdict(741.60, [("upstox", 741.60), ("dhan", 741.60)]) == ""

    def test_names_the_broker_a_bad_print_came_from(self) -> None:
        """IRB 2026-03-30: the lake inherited Dhan's value, not Upstox's."""
        assert _verdict(11.35, [("upstox", 22.67), ("dhan", 11.35)]) == "lake == dhan"

    def test_names_the_broker_when_it_is_the_other_one(self) -> None:
        assert _verdict(438.65, [("upstox", 438.65), ("dhan", 426.25)]) == "lake == upstox"

    def test_reports_a_rebased_series_with_its_ratio(self) -> None:
        """ANANDRATHI 2026-06-02: the lake sits at 2x both brokers (unadjusted)."""
        assert _verdict(3553.10, [("upstox", 1777.50), ("dhan", 1776.55)]) \
            == "LAKE != BOTH (2.00x)"

    def test_reports_a_value_no_broker_quotes(self) -> None:
        assert _verdict(999.0, [("upstox", 22.67), ("dhan", 11.35)]) \
            == "LAKE MATCHES NEITHER"

    def test_missing_inputs_are_not_findings(self) -> None:
        assert _verdict(None, [("upstox", 1.0)]) == ""
        assert _verdict(1.0, [("upstox", None)]) == ""

    def test_a_tick_of_slack_is_not_a_disagreement(self) -> None:
        """ANANDRATHI's post-split day differs by 80 paise across brokers."""
        assert _agrees(1811.40, 1810.60)
        assert not _agrees(22.67, 11.35)
