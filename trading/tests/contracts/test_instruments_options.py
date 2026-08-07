"""Instrument + options edge-branch contract tests — ported from v3.

Closes coverage gaps in instruments.py and options.py: Currency/Commodity
factories, Option float-strike, symbol normalization, and Expiry error branches.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from tradex_domain import (
    Commodity,
    Currency,
    Equity,
    Expiry,
    Index,
    Option,
    OptionPair,
    Price,
    SDKError,
)

# ---------------------------------------------------------------------------
# Instruments — factories + normalization
# ---------------------------------------------------------------------------


def test_currency_factory() -> None:
    usdinr = Currency.of("NSE", "USDINR")
    assert usdinr.asset_class.value == "CURRENCY"
    assert usdinr.instrument_type.value == "CURRENCY"


def test_commodity_factory() -> None:
    gold = Commodity.of("MCX", "GOLD")
    assert gold.asset_class.value == "COMMODITY"
    assert gold.exchange.value == "MCX"


def test_option_of_with_float_strike() -> None:
    opt = Option.of("NFO", "NIFTY", date(2026, 7, 30), 25000.5, "CE")
    assert opt.strike == Decimal("25000.5")
    assert opt.right == "CE"
    assert opt.option_type == "CE"


def test_option_rejects_invalid_right_on_construction() -> None:
    with pytest.raises(ValueError):
        Option.of("NFO", "NIFTY", date(2026, 7, 30), Decimal("25000"), "XX")


def test_instrument_symbol_normalization() -> None:
    eq = Equity.of("NSE", " reliance ")
    assert eq.symbol == "RELIANCE"
    assert eq.underlying == "RELIANCE"


# ---------------------------------------------------------------------------
# Expiry — atm/otm/itm error branches + offsets
# ---------------------------------------------------------------------------


def _expiry(reference: Price | None = None) -> Expiry:
    nifty = Index.of("NSE", "NIFTY")
    d = date(2026, 7, 30)
    pairs = tuple(
        OptionPair(
            call=Option.of("NFO", "NIFTY", d, Decimal(str(s)), "CE"),
            put=Option.of("NFO", "NIFTY", d, Decimal(str(s)), "PE"),
            strike=Price(value=Decimal(str(s))),
        )
        for s in (24800, 24900, 25000, 25100, 25200)
    )
    return Expiry(
        underlying=nifty,
        expiry_date=d,
        pairs=pairs,
        reference_price=reference,
    )


def test_expiry_atm_requires_reference_price() -> None:
    with pytest.raises(SDKError):
        _expiry(reference=None).atm(0)


def test_expiry_atm_requires_pairs() -> None:
    exp = Expiry(
        underlying=Index.of("NSE", "NIFTY"),
        expiry_date=date(2026, 7, 30),
        reference_price=Price(value=Decimal("25000")),
    )
    with pytest.raises(SDKError):
        exp.atm(0)


def test_expiry_atm_offset_out_of_range() -> None:
    exp = _expiry(reference=Price(value=Decimal("25000")))
    with pytest.raises(IndexError):
        exp.atm(10)


def test_expiry_otm_requires_reference_price() -> None:
    with pytest.raises(SDKError):
        _expiry(reference=None).otm(2)


def test_expiry_itm_requires_reference_price() -> None:
    with pytest.raises(SDKError):
        _expiry(reference=None).itm(2)


def test_expiry_atm_with_offset() -> None:
    exp = _expiry(reference=Price(value=Decimal("25000")))
    assert exp.atm(1).strike.value == 25100
    assert exp.atm(-1).strike.value == 24900


def test_option_pair_serialization_round_trip() -> None:
    pair = _expiry(reference=Price(value=Decimal("25000"))).pairs[0]
    assert OptionPair.from_dict(pair.to_dict()) == pair
