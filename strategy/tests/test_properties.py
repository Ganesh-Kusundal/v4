"""Property-based invariants for the two safety-critical pure functions.

Real-money correctness rests on two properties that must hold for *every* input,
not just the examples a unit test happens to pick:

* the sizer never returns a quantity whose notional exceeds ``max_order_value``;
* ``protective_request`` brackets a position only when the stop/target geometry
  is valid for the side, and never attaches a lone/invalid leg otherwise.

Hypothesis generates the adversarial cases (huge price, tiny ceiling, reversed
levels) that hand-written tests miss.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st
from tradex_domain.enums import OrderSide
from tradex_domain.execution import BracketOrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.strategy import Signal
from tradex_domain.value_objects import Price, Quantity
from tradex_strategy.core.brackets import is_valid_bracket, protective_request
from tradex_strategy.core.sizing import PositionSizer

_INST = Equity.of("NSE", "TEST")
_TS = datetime(2026, 1, 1, tzinfo=UTC)

_DEC = dict(
    min_value=Decimal("0.01"),
    max_value=Decimal("1000000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)

_MAX_OV = st.decimals(
    min_value=Decimal("1"),
    max_value=Decimal("10000000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)


def _signal(side: OrderSide, qty: int, metadata: dict | None = None) -> Signal:
    return Signal(
        instrument=_INST,
        direction=side,
        strength=1.0,
        reason="prop",
        quantity=Quantity(Decimal(qty)),
        metadata=metadata or {},
        timestamp=_TS,
    )


@given(
    max_order_value=_MAX_OV,
    price=st.decimals(**_DEC),
    qty=st.integers(min_value=1, max_value=1_000_000),
)
def test_sizer_never_exceeds_the_notional_ceiling(
    max_order_value: Decimal, price: Decimal, qty: int
) -> None:
    sizer = PositionSizer(max_order_value=max_order_value)
    decision = sizer.size(_signal(OrderSide.BUY, qty), reference_price=Price(price))
    if decision.quantity is None:
        # Refused: one lot is unaffordable inside the ceiling. Acceptable.
        return
    assert decision.quantity.value >= 0
    assert decision.quantity.value * price <= max_order_value, (
        f"{decision.quantity.value} x {price} = "
        f"{decision.quantity.value * price} > ceiling {max_order_value}"
    )


@given(
    side=st.sampled_from([OrderSide.BUY, OrderSide.SELL]),
    entry=st.decimals(**_DEC),
    stop=st.decimals(**_DEC),
    target=st.decimals(**_DEC),
)
def test_is_valid_bracket_matches_strict_geometry(
    side: OrderSide, entry: Decimal, stop: Decimal, target: Decimal
) -> None:
    expected = (
        stop < entry < target
        if side is OrderSide.BUY
        else target < entry < stop
    )
    assert is_valid_bracket(side, Price(entry), Price(stop), Price(target)) is expected


@given(
    side=st.sampled_from([OrderSide.BUY, OrderSide.SELL]),
    entry=st.decimals(**_DEC),
    stop=st.decimals(**_DEC),
    target=st.decimals(**_DEC),
)
def test_protective_request_brackets_only_valid_geometry(
    side: OrderSide, entry: Decimal, stop: Decimal, target: Decimal
) -> None:
    signal = _signal(
        side,
        10,
        {"stop_loss_price": stop, "target_price": target},
    )
    request = protective_request(
        signal=signal, entry=Price(entry), quantity=Quantity(Decimal("10"))
    )
    valid = is_valid_bracket(side, Price(entry), Price(stop), Price(target))
    if valid:
        assert isinstance(request, BracketOrderRequest)
        assert request.stop_loss_price is not None
        assert request.target_price is not None
    else:
        # Invalid geometry degrades to unprotected (no lone leg), never raises.
        assert not isinstance(request, BracketOrderRequest)
        assert getattr(request, "stop_loss_price", None) is None
        assert getattr(request, "target_price", None) is None
