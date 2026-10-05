"""Gross/net exposure limits (optional account-level caps).

``RiskManager`` denies an order when the book *resulting from it* would
push total gross exposure past ``max_gross_exposure`` or absolute net
exposure past ``max_net_exposure``. Both default to ``None`` (no cap),
so the pre-existing behaviour is unchanged when they are unset.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_config.schema import RiskConfig
from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.execution import OrderRequest, Position
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Money, Price, Quantity
from tradex_execution.risk import RiskManager

_INST = Equity.of("NSE", "TEST")


def _request(side: OrderSide, qty: str = "100") -> OrderRequest:
    return OrderRequest(
        instrument=_INST,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
    )


def _position(qty: str = "0") -> Position:
    return Position(
        instrument=_INST,
        quantity=Quantity(value=Decimal(qty)),
        avg_price=Price(value=Decimal("100")),
        realized_pnl=Money(amount=Decimal("0")),
        unrealized_pnl=Money(amount=Decimal("0")),
    )


def _rm(qty: str = "100", gross: str | None = None, net: str | None = None) -> RiskManager:
    return RiskManager(
        positions_provider=lambda: [_position(qty)],
        max_gross_exposure=Decimal(gross) if gross is not None else None,
        max_net_exposure=Decimal(net) if net is not None else None,
    )


# ---------------------------------------------------------------------------
# Gross limit
# ---------------------------------------------------------------------------


def test_gross_limit_denies_when_resulting_gross_exceeds() -> None:
    # Book: long 100 @ 100 = 10,000 gross. BUY 60 → 16,000 > 15,000 cap.
    rm = _rm(qty="100", gross="15000")
    assert rm.check(_request(OrderSide.BUY, qty="60")) is False


def test_gross_limit_allows_when_resulting_gross_stays_under() -> None:
    # BUY 40 → 14,000 <= 15,000 cap.
    rm = _rm(qty="100", gross="15000")
    assert rm.check(_request(OrderSide.BUY, qty="40")) is True


def test_gross_limit_allows_exactly_at_cap() -> None:
    # BUY 50 → 15,000 == cap: the limit is a ceiling, not a trigger.
    rm = _rm(qty="100", gross="15000")
    assert rm.check(_request(OrderSide.BUY, qty="50")) is True


def test_gross_limit_counts_both_sides() -> None:
    # Short 100 @ 100 = 10,000 gross. SELL 60 → 16,000 > 15,000 cap.
    rm = _rm(qty="-100", gross="15000")
    assert rm.check(_request(OrderSide.SELL, qty="60")) is False


def test_gross_limit_never_blocks_a_reduction() -> None:
    # SELL 40 against long 100 → gross 10,000 → 6,000: a reduction, allowed.
    rm = _rm(qty="100", gross="15000")
    assert rm.check(_request(OrderSide.SELL, qty="40")) is True


def test_gross_deny_reason_is_exact() -> None:
    rm = _rm(qty="100", gross="15000")
    result = rm.check_order(_request(OrderSide.BUY, qty="60"))
    assert result.approved is False
    assert result.reason == "max_gross_exposure_exceeded"


# ---------------------------------------------------------------------------
# Net limit
# ---------------------------------------------------------------------------


def test_net_limit_denies_when_resulting_net_exceeds() -> None:
    # Long 100 → net +10,000. BUY 60 → net +16,000 > 15,000 cap.
    rm = _rm(qty="100", net="15000")
    assert rm.check(_request(OrderSide.BUY, qty="60")) is False


def test_net_limit_denies_a_deepening_short() -> None:
    # Short 100 → net -10,000. SELL 60 → net -16,000, abs > 15,000 cap.
    rm = _rm(qty="-100", net="15000")
    assert rm.check(_request(OrderSide.SELL, qty="60")) is False


def test_net_limit_allows_when_resulting_net_stays_under() -> None:
    rm = _rm(qty="100", net="15000")
    assert rm.check(_request(OrderSide.BUY, qty="40")) is True


def test_net_limit_allows_exactly_at_cap() -> None:
    rm = _rm(qty="100", net="15000")
    assert rm.check(_request(OrderSide.BUY, qty="50")) is True


def test_net_limit_never_blocks_a_reduction() -> None:
    # SELL 40 against long 100 → net +10,000 → +6,000: abs shrinks, allowed.
    rm = _rm(qty="100", net="15000")
    assert rm.check(_request(OrderSide.SELL, qty="40")) is True


def test_net_deny_reason_is_exact() -> None:
    rm = _rm(qty="100", net="15000")
    result = rm.check_order(_request(OrderSide.BUY, qty="60"))
    assert result.approved is False
    assert result.reason == "max_net_exposure_exceeded"


# ---------------------------------------------------------------------------
# No-limit default (backward compat)
# ---------------------------------------------------------------------------


def test_no_limits_set_means_no_cap() -> None:
    # Default construction: neither cap is set, any size passes.
    rm = RiskManager(positions_provider=lambda: [_position("100")])
    assert rm.check(_request(OrderSide.BUY, qty="100000")) is True


def test_risk_config_exposure_limits_default_to_none() -> None:
    cfg = RiskConfig()
    assert cfg.max_gross_exposure is None
    assert cfg.max_net_exposure is None


def test_risk_config_coerces_exposure_limits_to_decimal() -> None:
    cfg = RiskConfig(max_gross_exposure=15000, max_net_exposure="20000.5")
    assert cfg.max_gross_exposure == Decimal("15000")
    assert cfg.max_net_exposure == Decimal("20000.5")
    assert isinstance(cfg.max_gross_exposure, Decimal)
    assert isinstance(cfg.max_net_exposure, Decimal)


def test_no_position_book_falls_back_to_incoming_notional() -> None:
    # No book: the order's own notional is the whole resulting exposure.
    rm = RiskManager(max_gross_exposure=Decimal("5000"))
    assert rm.check(_request(OrderSide.BUY, qty="40")) is True  # 4,000
    assert rm.check(_request(OrderSide.BUY, qty="60")) is False  # 6,000
