"""Value-object + serialization contract tests — ported from v3.

Closes coverage gaps in value_objects.py and serialization.py:
InstrumentId construction/parse edge cases, display/str formatting,
value-object type guards, and the generic to_dict/from_dict coercion
+ marker-resolution branches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

import pytest
from tradex_domain import (
    CorrelationId,
    Equity,
    InstrumentId,
    Money,
    Price,
    Quantity,
    SDKError,
)
from tradex_domain.serialization import Serializable, from_dict, to_dict

# ---------------------------------------------------------------------------
# InstrumentId — construction edge cases
# ---------------------------------------------------------------------------


def test_instrument_id_rejects_invalid_exchange() -> None:
    with pytest.raises(ValueError):
        InstrumentId("BOGUS", "RELIANCE")


def test_instrument_id_rejects_invalid_right() -> None:
    with pytest.raises(ValueError):
        InstrumentId(exchange="NSE", underlying="RELIANCE", right="XX")


def test_instrument_id_strike_coercion() -> None:
    iid = InstrumentId("NFO", "NIFTY", date(2026, 7, 30), strike="25000", right="CE")
    assert iid.strike == Decimal("25000")


def test_instrument_id_normalizes_fields() -> None:
    iid = InstrumentId.equity(" nse ", " reliance ")
    assert iid.exchange == "NSE"
    assert iid.underlying == "RELIANCE"


# ---------------------------------------------------------------------------
# InstrumentId.parse — variants + malformed input
# ---------------------------------------------------------------------------


def test_instrument_id_parse_variants() -> None:
    assert InstrumentId.parse("NSE:RELIANCE") == InstrumentId.equity("NSE", "RELIANCE")
    assert InstrumentId.parse("NFO:NIFTY:20260730") == InstrumentId.future(
        "NFO", "NIFTY", date(2026, 7, 30)
    )
    assert InstrumentId.parse("NFO:NIFTY:20260730:FUT") == InstrumentId.future(
        "NFO", "NIFTY", date(2026, 7, 30)
    )
    assert InstrumentId.parse("NFO:NIFTY:20260730:25000:CE") == InstrumentId.option(
        "NFO", "NIFTY", date(2026, 7, 30), Decimal("25000"), "CE"
    )


def test_instrument_id_parse_rejects_malformed() -> None:
    with pytest.raises(ValueError):
        InstrumentId.parse("NSE")  # no colon
    with pytest.raises(ValueError):
        InstrumentId.parse("NFO:NIFTY:notadate")  # 8-char bad expiry
    with pytest.raises(ValueError):
        InstrumentId.parse("NFO:NIFTY:20260730:notanumber")  # bad strike


def test_instrument_id_parse_rejects_short_malformed_segment() -> None:
    with pytest.raises(ValueError):
        InstrumentId.parse("NFO:NIFTY:baddate")


def test_instrument_id_parse_infer_future_without_right() -> None:
    iid = InstrumentId.parse("NFO:NIFTY:20260730")
    assert iid.right == "FUT"
    assert iid.expiry == date(2026, 7, 30)


# ---------------------------------------------------------------------------
# __str__ formatting
# ---------------------------------------------------------------------------


def test_instrument_id_str_formatting() -> None:
    assert str(InstrumentId.equity("NSE", "RELIANCE")) == "NSE:RELIANCE"
    assert str(InstrumentId.future("NFO", "NIFTY", date(2026, 7, 30))) == "NFO:NIFTY:20260730:FUT"
    assert (
        str(InstrumentId.option("NFO", "NIFTY", date(2026, 7, 30), Decimal("25000"), "CE"))
        == "NFO:NIFTY:20260730:25000:CE"
    )
    dec = InstrumentId.option("NFO", "NIFTY", date(2026, 7, 30), Decimal("25000.5"), "CE")
    assert str(dec) == "NFO:NIFTY:20260730:25000.5:CE"


# ---------------------------------------------------------------------------
# Value-object type guards
# ---------------------------------------------------------------------------


def test_price_requires_decimal() -> None:
    with pytest.raises(TypeError):
        Price(value=100)  # type: ignore[arg-type]


def test_quantity_requires_decimal() -> None:
    with pytest.raises(TypeError):
        Quantity(value=10)  # type: ignore[arg-type]


def test_money_requires_decimal() -> None:
    with pytest.raises(TypeError):
        Money(amount=100)  # type: ignore[arg-type]


def test_correlation_id_uuid_and_str() -> None:
    uuid_id = CorrelationId(value=UUID("12345678-1234-5678-1234-567812345678"))
    assert str(uuid_id.value) == "12345678-1234-5678-1234-567812345678"
    str_id = CorrelationId(value="corr-1")
    assert str_id.value == "corr-1"
    assert CorrelationId.from_dict(uuid_id.to_dict()) == uuid_id


# ---------------------------------------------------------------------------
# Serialization — generic to_dict/from_dict branches
# ---------------------------------------------------------------------------


class _Shade(Enum):
    RED = "red"
    BLUE = "blue"


@dataclass(frozen=True, slots=True)
class _Nested(Serializable):
    amount: Decimal
    ts: datetime


@dataclass(frozen=True, slots=True)
class _KitchenSink(Serializable):
    price: Price
    nested: _Nested
    pairs: tuple[Price, Quantity]
    tags: list[str]
    meta: dict[str, object]
    shade: _Shade
    uid: UUID
    day: date


def _sink() -> _KitchenSink:
    return _KitchenSink(
        price=Price(value=Decimal("100.5")),
        nested=_Nested(amount=Decimal("9.99"), ts=datetime(2026, 7, 30, 10, 0, tzinfo=UTC)),
        pairs=(Price(value=Decimal("10")), Quantity(value=Decimal("2"))),
        tags=["a", "b"],
        meta={"k": 1, "nested": {"x": "y"}},
        shade=_Shade.RED,
        uid=UUID("12345678-1234-5678-1234-567812345678"),
        day=date(2026, 7, 30),
    )


def test_generic_serialization_round_trip() -> None:
    sink = _sink()
    restored = _KitchenSink.from_dict(sink.to_dict())
    assert restored == sink


def test_to_dict_unsupported_type_raises() -> None:
    with pytest.raises(SDKError):
        to_dict({1, 2, 3})  # set is not serializable


def test_from_dict_requires_dataclass() -> None:
    with pytest.raises(SDKError):
        from_dict(int, {})  # type: ignore[arg-type]


def test_from_dict_resolves_concrete_subclass_marker() -> None:
    eq = Equity.of("NSE", "RELIANCE")
    data = eq.to_dict()
    assert data["__type__"] == "tradex_domain.instruments.Equity"
    assert Equity.from_dict(data) == eq


def test_from_dict_rejects_foreign_marker_and_falls_back() -> None:
    eq = Equity.of("NSE", "RELIANCE")
    data = eq.to_dict()
    data["__type__"] = "os.system"  # non-tradex marker -> refused
    restored = Equity.from_dict(data)
    assert restored == eq


def test_from_dict_ignores_unresolvable_marker() -> None:
    price = Price(value=Decimal("5"))
    data = price.to_dict()
    data["__type__"] = "tradex_domain.nonexistent.Thing"
    assert Price.from_dict(data) == price


def test_decimal_datetime_date_uuid_enum_coercion() -> None:
    sink = _sink()
    assert sink.to_dict()["nested"]["amount"] == "9.99"
    assert sink.to_dict()["shade"] == "red"
    assert sink.to_dict()["uid"] == "12345678-1234-5678-1234-567812345678"
    assert sink.to_dict()["day"] == "2026-07-30"
