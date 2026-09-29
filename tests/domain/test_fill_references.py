"""Tests for fill_references."""
from tradex_domain.fill_references import FillReference


def test_enum_has_four_members():
    assert len(FillReference) == 4


def test_member_values():
    assert FillReference.NEXT_OPEN.value == "next_open"
    assert FillReference.SIGNAL_CLOSE.value == "signal_close"
    assert FillReference.BOOK_MATCHING.value == "book_matching"
    assert FillReference.SLIPPAGE_MODEL.value == "slippage_model"


def test_lookup_by_value():
    assert FillReference("next_open") is FillReference.NEXT_OPEN
    assert FillReference("signal_close") is FillReference.SIGNAL_CLOSE
    assert FillReference("book_matching") is FillReference.BOOK_MATCHING
    assert FillReference("slippage_model") is FillReference.SLIPPAGE_MODEL


def test_members_are_unique():
    values = [m.value for m in FillReference]
    assert len(values) == len(set(values))
