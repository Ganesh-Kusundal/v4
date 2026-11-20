"""P0.1 — size/ADV-aware market-impact slippage.

A fixed-fraction slippage model charges a 1-lot order and a position that is a
third of the day's volume the same. ``MarketImpactSlippageModel`` grows the
impact with the square root of participation (``quantity / adv``), caps it, and
leaves the price unchanged when liquidity is unknown. The ``FillModel`` must
prefer its richer ``apply_for_request`` hook (instrument/timestamp aware) over
the plain ``apply`` protocol.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, OrderType
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_execution.fill_model import FillModel
from tradex_execution.slippage import MarketImpactSlippageModel

_INST = Equity.of("NSE", "RELIANCE")


def _request(
    *, side: OrderSide = OrderSide.BUY, qty: str = "100", price: str = "100"
) -> OrderRequest:
    return OrderRequest(
        instrument=_INST,
        side=side,
        order_type=OrderType.MARKET,
        quantity=Quantity(Decimal(qty)),
        price=Price(Decimal(price)),
    )


def test_impact_follows_the_square_root_of_participation() -> None:
    # adv 10_000, qty 100 -> ratio 0.01 -> sqrt 0.1 -> frac 0.01.
    model = MarketImpactSlippageModel(
        adv_provider=lambda instrument, ts: Decimal("10000"),
        impact_coef=Decimal("0.1"),
    )
    buy = model.apply_for_request(Price(Decimal("100")), _request())
    sell = model.apply_for_request(
        Price(Decimal("100")), _request(side=OrderSide.SELL)
    )
    assert buy.value == pytest.approx(Decimal("101"))
    assert sell.value == pytest.approx(Decimal("99"))


def test_impact_is_capped_at_max_fraction() -> None:
    # ratio 1 -> frac 1 -> capped at 0.05, so a huge order moves price 5%.
    model = MarketImpactSlippageModel(
        adv_provider=lambda instrument, ts: Decimal("100"),
        impact_coef=Decimal("1"),
        max_fraction=Decimal("0.05"),
    )
    buy = model.apply_for_request(Price(Decimal("100")), _request(qty="100"))
    assert buy.value == pytest.approx(Decimal("105"))


def test_unknown_liquidity_leaves_the_price_unchanged() -> None:
    model = MarketImpactSlippageModel(
        adv_provider=lambda instrument, ts: Decimal("0")
    )
    filled = model.apply_for_request(Price(Decimal("100")), _request())
    assert filled.value == Decimal("100")


def test_default_adv_drives_the_plain_protocol_path() -> None:
    # No instrument context: the protocol ``apply`` uses ``default_adv``.
    model = MarketImpactSlippageModel(
        adv_provider=None,
        default_adv=Decimal("10000"),
        impact_coef=Decimal("0.1"),
    )
    buy = model.apply(Price(Decimal("100")), OrderSide.BUY, Quantity(Decimal("100")))
    assert buy.value == pytest.approx(Decimal("101"))


def test_fill_model_prefers_apply_for_request() -> None:
    model = MarketImpactSlippageModel(
        adv_provider=lambda instrument, ts: Decimal("10000"),
        impact_coef=Decimal("0.1"),
    )
    # resolve_fill_price uses the request price (2500) as the base.
    filled = FillModel(slippage_model=model).resolve_fill_price(
        _request(qty="100", price="2500")
    )
    assert filled.value == pytest.approx(Decimal("2525"))
