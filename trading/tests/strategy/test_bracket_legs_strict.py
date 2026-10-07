"""Bracket legs: invalid or incomplete legs degrade to unprotected, loudly.

Documented contract (CLAUDE.md): the levels are a pair; a lone leg is dropped
loudly, an invalid pair degrades to an unprotected order rather than raising
mid-run, and nothing is ever inferred from a missing level. A strategy
declaration mistake must not discard a whole backtest or drop a live order.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from tradex_domain.enums import OrderSide
from tradex_domain.instruments import Equity
from tradex_domain.strategy import Signal
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


def test_invalid_bracket_degrades_to_unprotected(caplog) -> None:
    """A pair that does not bracket the entry submits unprotected, with a warning."""
    signal = _signal(stop="90", target="95")  # target below entry
    with caplog.at_level(logging.WARNING):
        req = protective_request(
            signal=signal,
            entry=Price(value=Decimal("100")),
            quantity=Quantity(value=Decimal("10")),
        )
    assert req.__class__.__name__ == "OrderRequest"
    assert req.stop_loss_price is None
    assert req.target_price is None
    assert "do not bracket the entry" in caplog.text


def test_single_leg_degrades_to_unprotected(caplog) -> None:
    """A lone leg is dropped loudly — never fabricated into a pair."""
    signal = _signal(stop="90", target=None)
    with caplog.at_level(logging.WARNING):
        req = protective_request(
            signal=signal,
            entry=Price(value=Decimal("100")),
            quantity=Quantity(value=Decimal("10")),
        )
    assert req.__class__.__name__ == "OrderRequest"
    assert req.stop_loss_price is None
    assert req.target_price is None
    assert "only one protective level" in caplog.text


def test_valid_bracket_returns_bracket_request() -> None:
    signal = _signal(stop="90", target="110")
    req = protective_request(
        signal=signal,
        entry=Price(value=Decimal("100")),
        quantity=Quantity(value=Decimal("10")),
    )
    assert req.__class__.__name__ == "BracketOrderRequest"
    assert req.stop_loss_price.value == Decimal("90")
    assert req.target_price.value == Decimal("110")
