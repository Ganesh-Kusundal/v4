"""Pure Risk Engine — stateless, deterministic order validation.

Validates orders against risk constraints without mutable state or side effects.
Same inputs always produce same outputs. No mutation of inputs.

Design:
    - RiskConfig: immutable configuration dataclass
    - RiskResult: immutable result dataclass
    - RiskEngine: pure function wrapper (check_order, check_position_limit)

All numeric comparisons use Decimal for precision (no float rounding issues).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Optional

from tradex_trading.events.projectors import OrderView, PositionView


# =============================================================================
# Configuration
# =============================================================================


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Immutable risk configuration.

    Attributes:
        max_order_value: Maximum value per order (quantity × price).
        max_position_value: Maximum value per position (quantity × avg_price).
        max_orders_per_minute: Maximum number of orders allowed in a 60-second window.
        max_daily_loss: Daily loss limit (reserved for future P&L tracking).
        allowed_instruments: Optional allowlist of instruments. None means all allowed.
    """

    max_order_value: float
    max_position_value: float
    max_orders_per_minute: int
    max_daily_loss: float
    allowed_instruments: Optional[list[str]] = None


# =============================================================================
# Result
# =============================================================================


@dataclass(frozen=True, slots=True)
class RiskResult:
    """Immutable risk check result.

    Attributes:
        approved: True if the order/position passes all risk checks.
        reason: Human-readable reason for rejection. None if approved.
        risk_score: Normalized risk score from 0.0 (no risk) to 1.0 (max risk).
    """

    approved: bool
    reason: Optional[str]
    risk_score: float


# =============================================================================
# Risk Engine — pure functions
# =============================================================================


class RiskEngine:
    """Pure, stateless risk engine for order validation.

    All methods are pure functions: no mutation of inputs, no side effects,
    no internal state between calls. Deterministic by construction.
    """

    def __init__(self, config: RiskConfig) -> None:
        self._config = config

    @property
    def config(self) -> RiskConfig:
        """Access the immutable config."""
        return self._config

    # -------------------------------------------------------------------------
    # Order validation
    # -------------------------------------------------------------------------

    def check_order(
        self,
        order: OrderView,
        positions: dict[str, PositionView],
        recent_orders: list[datetime],
    ) -> RiskResult:
        """Validate an order against all risk constraints.

        Pure function — does not mutate any inputs.

        Args:
            order: The order to validate.
            positions: Current positions keyed by instrument.
            recent_orders: Timestamps of recently placed orders (for rate limiting).

        Returns:
            RiskResult indicating approval/rejection with reason and risk score.
        """
        max_order_value = Decimal(str(self._config.max_order_value))

        # 1. Market order (no price) — cannot determine value, reject.
        if order.price is None:
            return RiskResult(
                approved=False,
                reason="Market order rejected: price is None, cannot determine value",
                risk_score=1.0,
            )

        # 2. Instrument allowlist check.
        if self._config.allowed_instruments is not None:
            if order.instrument not in self._config.allowed_instruments:
                return RiskResult(
                    approved=False,
                    reason=f"Instrument not allowed: {order.instrument}",
                    risk_score=1.0,
                )

        # 3. Order value check.
        order_value = order.quantity * order.price
        if order_value > max_order_value:
            return RiskResult(
                approved=False,
                reason=(
                    f"Order value {order_value} exceeds max "
                    f"{self._config.max_order_value}"
                ),
                risk_score=1.0,
            )

        # 4. Rate limit check.
        if not self._check_rate_limit(recent_orders):
            return RiskResult(
                approved=False,
                reason=(
                    f"Rate limit exceeded: {len(recent_orders)} orders in "
                    f"last minute (max {self._config.max_orders_per_minute})"
                ),
                risk_score=1.0,
            )

        # All checks passed — compute risk score.
        risk_score = self._compute_order_risk_score(order_value, max_order_value)
        return RiskResult(approved=True, reason=None, risk_score=risk_score)

    # -------------------------------------------------------------------------
    # Position limit check
    # -------------------------------------------------------------------------

    def check_position_limit(
        self,
        instrument: str,
        new_quantity: Decimal,
        avg_price: Decimal,
    ) -> RiskResult:
        """Check if a position stays within the configured limit.

        Pure function — does not mutate any inputs.

        Args:
            instrument: The instrument being traded.
            new_quantity: The proposed new quantity (absolute value used).
            avg_price: The average entry price for the position.

        Returns:
            RiskResult indicating whether the position is within limits.
        """
        max_position_value = Decimal(str(self._config.max_position_value))
        position_value = abs(new_quantity) * avg_price

        if position_value > max_position_value:
            return RiskResult(
                approved=False,
                reason=(
                    f"Position value {position_value} for {instrument} exceeds "
                    f"max {self._config.max_position_value}"
                ),
                risk_score=1.0,
            )

        risk_score = self._compute_position_risk_score(position_value, max_position_value)
        return RiskResult(approved=True, reason=None, risk_score=risk_score)

    # -------------------------------------------------------------------------
    # Internal helpers (pure)
    # -------------------------------------------------------------------------

    def _check_rate_limit(self, recent_orders: list[datetime]) -> bool:
        """Check if recent orders are within the per-minute rate limit.

        Counts orders within the 60-second window ending at the most recent
        order timestamp (or current time if list is empty).
        """
        if not recent_orders:
            return True

        # Determine the reference time: use the most recent order time.
        # All datetimes must be tz-aware for correct comparison.
        reference_time = recent_orders[0]
        for ts in recent_orders:
            if ts > reference_time:
                reference_time = ts

        cutoff = reference_time - timedelta(minutes=1)
        count = sum(1 for ts in recent_orders if ts >= cutoff)
        return count <= self._config.max_orders_per_minute

    @staticmethod
    def _compute_order_risk_score(
        order_value: Decimal, max_order_value: Decimal
    ) -> float:
        """Compute risk score for an approved order.

        Linear scaling: risk_score = order_value / max_order_value, capped at 1.0.
        Zero value → 0.0.
        """
        if max_order_value <= 0:
            return 1.0
        score = float(order_value / max_order_value)
        return min(score, 1.0)

    @staticmethod
    def _compute_position_risk_score(
        position_value: Decimal, max_position_value: Decimal
    ) -> float:
        """Compute risk score for an approved position.

        Linear scaling: risk_score = position_value / max_position_value, capped at 1.0.
        Zero value → 0.0.
        """
        if max_position_value <= 0:
            return 1.0
        score = float(position_value / max_position_value)
        return min(score, 1.0)
