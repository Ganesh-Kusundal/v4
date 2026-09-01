"""Tests for Pure Risk Engine — stateless, deterministic order validation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradex_trading.events.projectors import OrderView, PositionView
from tradex_trading.events.risk_engine import RiskConfig, RiskEngine, RiskResult


# =============================================================================
# Helpers — build real views (no mocking)
# =============================================================================


def _order(
    order_id: str = "ord-001",
    instrument: str = "NSE:RELIANCE",
    side: str = "BUY",
    quantity: str = "10",
    price: str = "2500",
    status: str = "ACK",
    filled_quantity: str = "0",
    correlation_id: str = "corr-001",
) -> OrderView:
    return OrderView(
        order_id=order_id,
        instrument=instrument,
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price) if price else None,
        status=status,
        filled_quantity=Decimal(filled_quantity),
        correlation_id=correlation_id,
    )


def _position(
    instrument: str = "NSE:RELIANCE",
    quantity: str = "5",
    avg_price: str = "2500",
    realized_pnl: str = "0",
) -> PositionView:
    return PositionView(
        instrument=instrument,
        quantity=Decimal(quantity),
        avg_price=Decimal(avg_price),
        realized_pnl=Decimal(realized_pnl),
    )


@pytest.fixture
def base_config() -> RiskConfig:
    return RiskConfig(
        max_order_value=100_000.0,
        max_position_value=500_000.0,
        max_orders_per_minute=5,
        max_daily_loss=50_000.0,
        allowed_instruments=None,
    )


@pytest.fixture
def engine(base_config: RiskConfig) -> RiskEngine:
    return RiskEngine(base_config)


# =============================================================================
# RiskResult tests
# =============================================================================


class TestRiskResult:
    def test_approved_result_has_no_reason(self):
        result = RiskResult(approved=True, reason=None, risk_score=0.1)
        assert result.approved is True
        assert result.reason is None
        assert result.risk_score == 0.1

    def test_rejected_result_has_reason(self):
        result = RiskResult(approved=False, reason="Order value exceeds limit", risk_score=1.0)
        assert result.approved is False
        assert result.reason == "Order value exceeds limit"

    def test_risk_result_is_frozen(self):
        result = RiskResult(approved=True, reason=None, risk_score=0.5)
        with pytest.raises(AttributeError):
            result.approved = False  # type: ignore[misc]


# =============================================================================
# RiskConfig tests
# =============================================================================


class TestRiskConfig:
    def test_config_stores_values(self):
        config = RiskConfig(
            max_order_value=100_000.0,
            max_position_value=500_000.0,
            max_orders_per_minute=5,
            max_daily_loss=50_000.0,
            allowed_instruments=["NSE:RELIANCE", "NSE:TCS"],
        )
        assert config.max_order_value == 100_000.0
        assert config.max_position_value == 500_000.0
        assert config.max_orders_per_minute == 5
        assert config.max_daily_loss == 50_000.0
        assert config.allowed_instruments == ["NSE:RELIANCE", "NSE:TCS"]

    def test_config_allowed_instruments_defaults_to_none(self):
        config = RiskConfig(
            max_order_value=100_000.0,
            max_position_value=500_000.0,
            max_orders_per_minute=5,
            max_daily_loss=50_000.0,
        )
        assert config.allowed_instruments is None

    def test_config_is_frozen(self):
        config = RiskConfig(
            max_order_value=100_000.0,
            max_position_value=500_000.0,
            max_orders_per_minute=5,
            max_daily_loss=50_000.0,
        )
        with pytest.raises(AttributeError):
            config.max_order_value = 200_000.0  # type: ignore[misc]


# =============================================================================
# check_order — approval tests
# =============================================================================


class TestCheckOrderApproved:
    def test_approved_order_returns_approved(self, engine: RiskEngine):
        """Valid order within all limits → approved."""
        order = _order(quantity="10", price="2500")  # value = 25,000
        result = engine.check_order(order, positions={}, recent_orders=[])
        assert result.approved is True
        assert result.reason is None

    def test_approved_order_risk_score_between_0_and_1(self, engine: RiskEngine):
        """Risk score for approved order is in [0, 1)."""
        order = _order(quantity="10", price="2500")  # 25% of max_order_value
        result = engine.check_order(order, positions={}, recent_orders=[])
        assert 0.0 <= result.risk_score < 1.0

    def test_risk_score_scales_with_order_value(self, engine: RiskEngine):
        """Larger order relative to limit → higher risk score."""
        small = engine.check_order(_order(quantity="1", price="2500"), {}, [])
        large = engine.check_order(_order(quantity="9", price="2500"), {}, [])
        assert large.risk_score > small.risk_score

    def test_order_at_exact_limit_is_approved(self, engine: RiskEngine):
        """Order value == max_order_value → approved (boundary inclusive)."""
        order = _order(quantity="4", price="25000")  # value = 100,000
        result = engine.check_order(order, positions={}, recent_orders=[])
        assert result.approved is True

    def test_empty_positions_and_recent_orders(self, engine: RiskEngine):
        """No existing positions, no recent orders → approved."""
        order = _order()
        result = engine.check_order(order, positions={}, recent_orders=[])
        assert result.approved is True


# =============================================================================
# check_order — rejection tests
# =============================================================================


class TestCheckOrderRejected:
    def test_order_exceeding_max_value_rejected(self, engine: RiskEngine):
        """Order value > max_order_value → rejected."""
        order = _order(quantity="5", price="25000")  # value = 125,000 > 100,000
        result = engine.check_order(order, positions={}, recent_orders=[])
        assert result.approved is False
        assert result.reason is not None
        assert "order value" in result.reason.lower()

    def test_order_exceeding_max_value_risk_score_is_1(self, engine: RiskEngine):
        """Rejected order for value breach → risk_score == 1.0."""
        order = _order(quantity="5", price="25000")
        result = engine.check_order(order, positions={}, recent_orders=[])
        assert result.risk_score == 1.0

    def test_rate_limit_rejected(self, engine: RiskEngine):
        """Too many orders in the last minute → rejected."""
        now = datetime(2026, 1, 1, 9, 15, tzinfo=UTC)
        # 6 orders in the last minute, limit is 5
        recent_orders = [now - timedelta(seconds=i * 10) for i in range(6)]
        order = _order()
        result = engine.check_order(order, positions={}, recent_orders=recent_orders)
        assert result.approved is False
        assert result.reason is not None
        assert "rate" in result.reason.lower()

    def test_rate_limit_at_exact_boundary_approved(self, engine: RiskEngine):
        """Orders in window == max_orders_per_minute → approved (boundary inclusive)."""
        now = datetime(2026, 1, 1, 9, 15, tzinfo=UTC)
        recent_orders = [now - timedelta(seconds=i * 10) for i in range(5)]
        order = _order()
        result = engine.check_order(order, positions={}, recent_orders=recent_orders)
        assert result.approved is True

    def test_disallowed_instrument_rejected(self, base_config: RiskConfig):
        """Instrument not in allowed list → rejected."""
        config = RiskConfig(
            max_order_value=base_config.max_order_value,
            max_position_value=base_config.max_position_value,
            max_orders_per_minute=base_config.max_orders_per_minute,
            max_daily_loss=base_config.max_daily_loss,
            allowed_instruments=["NSE:RELIANCE", "NSE:TCS"],
        )
        engine = RiskEngine(config)
        order = _order(instrument="NSE:INFY")
        result = engine.check_order(order, positions={}, recent_orders=[])
        assert result.approved is False
        assert result.reason is not None
        assert "instrument" in result.reason.lower()

    def test_market_order_no_price_rejected(self, engine: RiskEngine):
        """Market order (price=None) → rejected (value unbounded)."""
        order = _order(price=None)
        result = engine.check_order(order, positions={}, recent_orders=[])
        assert result.approved is False
        assert result.reason is not None

    def test_rejected_order_risk_score_between_0_and_1(self, engine: RiskEngine):
        """Risk score for rejected order is still in [0, 1]."""
        order = _order(quantity="5", price="25000")
        result = engine.check_order(order, positions={}, recent_orders=[])
        assert 0.0 <= result.risk_score <= 1.0


# =============================================================================
# check_position_limit tests
# =============================================================================


class TestCheckPositionLimit:
    def test_position_within_limit_approved(self, engine: RiskEngine):
        """Position value ≤ max_position_value → approved."""
        result = engine.check_position_limit("NSE:RELIANCE", Decimal("10"), Decimal("2500"))
        assert result.approved is True
        assert result.reason is None

    def test_position_limit_rejected(self, engine: RiskEngine):
        """Position value > max_position_value → rejected."""
        result = engine.check_position_limit("NSE:RELIANCE", Decimal("250"), Decimal("2500"))
        # 250 × 2500 = 625,000 > 500,000
        assert result.approved is False
        assert result.reason is not None
        assert "position" in result.reason.lower()

    def test_position_at_exact_limit_approved(self, engine: RiskEngine):
        """Position value == max_position_value → approved (boundary inclusive)."""
        result = engine.check_position_limit("NSE:RELIANCE", Decimal("200"), Decimal("2500"))
        # 200 × 2500 = 500,000
        assert result.approved is True

    def test_position_limit_risk_score_between_0_and_1(self, engine: RiskEngine):
        """Risk score for position check is in [0, 1]."""
        result = engine.check_position_limit("NSE:RELIANCE", Decimal("250"), Decimal("2500"))
        assert 0.0 <= result.risk_score <= 1.0

    def test_position_limit_risk_score_scales_with_value(self, engine: RiskEngine):
        """Larger position → higher risk score."""
        small = engine.check_position_limit("NSE:RELIANCE", Decimal("10"), Decimal("2500"))
        large = engine.check_position_limit("NSE:RELIANCE", Decimal("100"), Decimal("2500"))
        assert large.risk_score > small.risk_score

    def test_position_limit_uses_absolute_quantity(self, engine: RiskEngine):
        """Short position (negative quantity) uses absolute value."""
        short = engine.check_position_limit("NSE:RELIANCE", Decimal("-250"), Decimal("2500"))
        long = engine.check_position_limit("NSE:RELIANCE", Decimal("250"), Decimal("2500"))
        assert short.approved == long.approved
        assert short.risk_score == long.risk_score


# =============================================================================
# Purity & determinism tests
# =============================================================================


class TestPurityAndDeterminism:
    def test_same_inputs_same_output(self, engine: RiskEngine):
        """Deterministic: identical inputs → identical output."""
        order = _order(quantity="10", price="2500")
        positions = {"NSE:RELIANCE": _position()}
        recent_orders = [datetime(2026, 1, 1, 9, 14, tzinfo=UTC)]

        r1 = engine.check_order(order, positions, recent_orders)
        r2 = engine.check_order(order, positions, recent_orders)
        assert r1 == r2

    def test_engine_does_not_mutate_inputs(self, engine: RiskEngine):
        """Pure function: inputs are not mutated."""
        order = _order(quantity="10", price="2500")
        positions = {"NSE:RELIANCE": _position()}
        recent_orders = [datetime(2026, 1, 1, 9, 14, tzinfo=UTC)]

        # Snapshot
        pos_qty_before = positions["NSE:RELIANCE"].quantity
        recent_len_before = len(recent_orders)

        engine.check_order(order, positions, recent_orders)

        assert positions["NSE:RELIANCE"].quantity == pos_qty_before
        assert len(recent_orders) == recent_len_before

    def test_multiple_calls_no_state_leak(self, engine: RiskEngine):
        """Stateless: repeated calls don't accumulate state."""
        # Reject one order
        big_order = _order(quantity="5", price="25000")
        engine.check_order(big_order, {}, [])

        # Next valid order should still be approved
        valid_order = _order(order_id="ord-002", quantity="1", price="100")
        result = engine.check_order(valid_order, {}, [])
        assert result.approved is True


# =============================================================================
# Instrument allowlist tests
# =============================================================================


class TestInstrumentAllowlist:
    def test_allowed_instrument_approved(self, base_config: RiskConfig):
        """Instrument in allowed list → approved."""
        config = RiskConfig(
            max_order_value=100_000.0,
            max_position_value=500_000.0,
            max_orders_per_minute=5,
            max_daily_loss=50_000.0,
            allowed_instruments=["NSE:RELIANCE", "NSE:TCS"],
        )
        engine = RiskEngine(config)
        order = _order(instrument="NSE:TCS")
        result = engine.check_order(order, {}, [])
        assert result.approved is True

    def test_disallowed_instrument_with_valid_value_rejected(self, base_config: RiskConfig):
        """Valid order value but disallowed instrument → rejected."""
        config = RiskConfig(
            max_order_value=100_000.0,
            max_position_value=500_000.0,
            max_orders_per_minute=5,
            max_daily_loss=50_000.0,
            allowed_instruments=["NSE:RELIANCE"],
        )
        engine = RiskEngine(config)
        order = _order(instrument="NSE:INFY", quantity="1", price="100")
        result = engine.check_order(order, {}, [])
        assert result.approved is False

    def test_none_allowed_means_all_allowed(self, engine: RiskEngine):
        """allowed_instruments=None → any instrument allowed."""
        order = _order(instrument="NSE:ANYTHING")
        result = engine.check_order(order, {}, [])
        assert result.approved is True


# =============================================================================
# Risk score calculation tests
# =============================================================================


class TestRiskScore:
    def test_risk_score_zero_for_zero_value_order(self, engine: RiskEngine):
        """Zero-quantity order → risk_score == 0.0."""
        order = _order(quantity="0", price="2500")
        result = engine.check_order(order, {}, [])
        assert result.risk_score == 0.0

    def test_risk_score_monotonic_with_order_size(self, engine: RiskEngine):
        """Risk score increases monotonically with order value."""
        scores = []
        for qty in ["1", "2", "3", "4"]:
            order = _order(quantity=qty, price="2500")
            result = engine.check_order(order, {}, [])
            scores.append(result.risk_score)
        assert scores == sorted(scores)

    def test_risk_score_capped_at_1(self, engine: RiskEngine):
        """Risk score never exceeds 1.0 even for huge orders."""
        order = _order(quantity="1000", price="25000")  # 25M >> 100K
        result = engine.check_order(order, {}, [])
        assert result.risk_score == 1.0
