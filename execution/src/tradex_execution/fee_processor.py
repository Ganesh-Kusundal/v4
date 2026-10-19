"""Fee computation — extracted from ExecutionEngine.

Owns the brokerage accrual state and the capped-brokerage calculation.
"""

from __future__ import annotations

import threading
from decimal import Decimal
from typing import Any

from tradex_domain.execution import Fill


class FeeProcessor:
    """Computes per-fill fees with a capped-brokerage accrual.

    Parameters
    ----------
    fee_calculator:
        The engine's fee calculator. When ``None``, every fee is zero.
    """

    def __init__(self, fee_calculator: Any | None = None) -> None:
        self._fee_calculator = fee_calculator
        self._brokerage_accrued: dict[str, Decimal] = {}
        self._brokerage_lock = threading.Lock()

    @property
    def accrued(self) -> dict[str, Decimal]:
        """Per-order brokerage accrual read surface (H2: replay cannot re-derive it)."""
        return self._brokerage_accrued

    def compute_fee(self, fill: Fill) -> Decimal:
        """Compute *fill*'s fee and advance the capped-brokerage accrual.

        Separate from application so the charged amount is known *before* any
        state moves and can be persisted with the event. The ₹20 per-order cap
        accrues across a partial-fill sequence, so replay cannot re-derive this
        from a single fill (H2).
        """
        if self._fee_calculator is None:
            return Decimal(0)
        oid = fill.order_id.value if hasattr(fill.order_id, "value") else str(fill.order_id)
        with self._brokerage_lock:
            accrued = self._brokerage_accrued.get(oid, Decimal(0))
            fee, self._brokerage_accrued[oid] = self._fee_calculator.calculate_capped(
                fill, accrued,
            )
        return fee.amount

    def fee_for_fill(
        self, fill: Fill, recorded_fee_for: Any | None = None
    ) -> Decimal:
        """The fee to charge for *fill*: recorded if replaying, else computed.

        A replay source carries the fee actually charged on the original run.
        Using it keeps replayed history equal to the run it replays; recomputing
        from today's rate table would silently rewrite past costs.
        """
        if recorded_fee_for is not None:
            fee = recorded_fee_for(fill)
            if fee is not None:
                return fee
        return self.compute_fee(fill)
