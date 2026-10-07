"""Bracket legs: invalid or incomplete legs must raise, not drop silently.

A signal that declares protective levels that do not bracket the entry
must raise ValueError, not submit an unprotected order.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradex_domain.enums import OrderSide
from tradex_domain.strategy import Signal
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_strategy.core.brackets import protective_request


def _signal(stop: str | None, target: str | None) -> Signal:
    metadata = {}
    if stop is not None:
        metadata["stop_loss_price"] = Price(value=Decimal(stop))
    if target is not None:
        metadata["target_price"] = Price(value=Decimal(target))
    return Signal(
        instrument=Equity.of("NSE", "TEST"),
        direction=OrderSide.BUY,
        strength=1.0,
        reason="test",
        metadata=metadata,
    )


def test_invalid_bracket_raises() -> None:
    signal = _signal(stop="90", target="95")  # target below entry
    with pytest.raises(ValueError):
        protective_request(
            signal=signal,
            entry=Price(value=Decimal("100")),
            quantity=Quantity(value=Decimal("10")),
        )


def test_single_leg_raises() -> None:
    signal = _signal(stop="90", target=None)
    with pytest.raises(ValueError):
        protective_request(
            signal=signal,
            entry=Price(value=Decimal("100")),
            quantity=Quantity(value=Decimal("10")),
        )


def test_valid_bracket_returns_bracket_request() -> None:
    signal = _signal(stop="90", target="110")
    req = protective_request(
        signal=signal,
        entry=Price(value=Decimal("100")),
        quantity=Quantity(value=Decimal("10")),
    )
    assert req.__class__.__name__ == "BracketOrderRequest"
