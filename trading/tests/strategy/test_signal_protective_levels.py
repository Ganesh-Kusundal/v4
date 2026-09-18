"""The signal → order protective-level contract.

A strategy declares a stop and a target on the signal it emits; the engine
carries them onto the order. These tests pin the three decisions that contract
makes, each of which is a place a plausible implementation would be wrong:

- the legs are a **pair** — a lone stop is dropped, not shipped as protection
  the venue cannot carry;
- the geometry is validated against the price the order **actually fills at**,
  and an invalid pair degrades to an unprotected order instead of raising in the
  middle of a run;
- an unusable value (``None``, a string, ``NaN``) is ignored, because strategy
  arithmetic on a thin bar produces those and the engine must not die on them.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain import OrderSide, Signal
from tradex_domain.enums import OrderType
from tradex_domain.execution import BracketOrderRequest, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.strategy.core.brackets import (
    STOP_KEY,
    TARGET_KEY,
    declared_levels,
    protective_request,
)


def _signal(side: OrderSide = OrderSide.BUY, **metadata: object) -> Signal:
    return Signal(
        instrument=Equity.of("NSE", "RELIANCE"),
        direction=side,
        strength=1.0,
        reason="test",
        metadata=dict(metadata),
    )


def _request(signal: Signal, entry: str = "2500") -> OrderRequest:
    return protective_request(
        signal=signal,
        entry=Price(value=Decimal(entry)),
        quantity=Quantity(value=Decimal("10")),
    )


def test_a_valid_pair_rides_a_bracket_request() -> None:
    request = _request(_signal(stop_loss_price=2450.0, target_price=2600.0))

    # The domain's own bracket type, not a plain request with spare fields: the
    # identity is what routes the order to the venue's super-order endpoint.
    assert isinstance(request, BracketOrderRequest)
    assert request.stop_loss_price == Price(value=Decimal("2450.0"))
    assert request.target_price == Price(value=Decimal("2600.0"))
    assert request.price == Price(value=Decimal("2500"))


def test_a_short_is_bracketed_the_other_way_round() -> None:
    request = _request(
        _signal(OrderSide.SELL, stop_loss_price=2600.0, target_price=2400.0)
    )

    assert isinstance(request, BracketOrderRequest)
    assert request.stop_loss_price == Price(value=Decimal("2600.0"))
    assert request.target_price == Price(value=Decimal("2400.0"))


def test_a_lone_level_is_dropped_rather_than_shipped() -> None:
    # The venue carries a stop only as a composite, so a lone leg would be
    # discarded by the adapter while the strategy believed it was protected.
    request = _request(_signal(stop_loss_price=2450.0))

    assert type(request) is OrderRequest
    assert request.stop_loss_price is None
    assert request.target_price is None


def test_levels_are_validated_against_the_price_that_actually_fills() -> None:
    """The declared pair is checked against the fill price, not the signal bar.

    ``next_open`` fills at the following bar's open, so a gap can leave the
    declared stop above the entry a long actually got. The order degrades to
    unprotected; it must not raise, and it must not carry a stop that is above
    its own entry.
    """
    signal = _signal(stop_loss_price=2450.0, target_price=2600.0)
    request = _request(signal, entry="2700")

    assert type(request) is OrderRequest
    assert request.stop_loss_price is None
    assert request.price == Price(value=Decimal("2700"))


def test_unusable_values_are_ignored_not_fatal() -> None:
    for bad in ("", "abc", float("nan"), float("inf"), 0, -3):
        stop, target = declared_levels({STOP_KEY: bad, TARGET_KEY: 2600.0})
        assert stop is None, f"{bad!r} must not become a price"
        assert target == Price(value=Decimal("2600.0"))

    # One unusable half leaves no pair, so the order is plainly unprotected.
    request = _request(_signal(stop_loss_price=None, target_price=2600.0))
    assert type(request) is OrderRequest


def test_a_price_and_a_decimal_are_accepted_like_a_float() -> None:
    stop, target = declared_levels(
        {STOP_KEY: Price(value=Decimal("2450")), TARGET_KEY: Decimal("2600")}
    )

    assert stop == Price(value=Decimal("2450"))
    assert target == Price(value=Decimal("2600"))


def test_metadata_that_is_not_a_mapping_declares_nothing() -> None:
    assert declared_levels(None) == (None, None)
    assert declared_levels("stop_loss_price=2450") == (None, None)

    request = _request(_signal())
    assert type(request) is OrderRequest
    assert request.order_type is OrderType.MARKET


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({}, (None, None)),
        ({STOP_KEY: 2450.0}, (Price(value=Decimal("2450.0")), None)),
        ({TARGET_KEY: 2600.0}, (None, Price(value=Decimal("2600.0")))),
    ],
)
def test_declared_levels_reads_each_key_independently(
    metadata: dict, expected: tuple
) -> None:
    assert declared_levels(metadata) == expected
