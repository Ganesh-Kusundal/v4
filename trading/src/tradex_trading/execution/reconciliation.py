"""Reconciliation engine — compares local vs broker state.

Side-effect-free drift detection for positions, orders, and funds.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from tradex_domain.execution import Account, Order, Position


class DriftSeverity(StrEnum):
    """Severity levels for reconciliation drift."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class DriftItem:
    """A single discrepancy between local and broker state.

    For position-level drift (from ``reconcile``), ``symbol`` /
    ``local_quantity`` / ``broker_quantity`` / ``diff`` are populated.
    For order / funds drift (from ``compare_orders`` / ``compare_funds``),
    ``kind`` / ``key`` / ``severity`` / ``reason`` / ``local`` / ``remote``
    are populated instead.
    """

    # Position-level fields (used by reconcile())
    symbol: str = ""
    local_quantity: Decimal = Decimal("0")
    broker_quantity: Decimal = Decimal("0")
    diff: Decimal = Decimal("0")
    # Extended fields (used by compare_orders / compare_funds)
    kind: str = "position"
    key: str = ""
    severity: DriftSeverity = DriftSeverity.LOW
    reason: str = ""
    local: Any = None
    remote: Any = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _order_key(order: Order) -> str:
    return order.order_id.value


class ReconciliationEngine:
    """Reconciles local state against broker snapshots.

    Produces DriftItems for any discrepancy in positions, orders, or funds.
    """

    def reconcile(
        self,
        local: list[Position],
        broker: list[Position],
    ) -> list[DriftItem]:
        """Compare local and broker positions, returning drift items."""
        local_map: dict[str, Position] = {
            p.instrument.symbol: p for p in local
        }
        broker_map: dict[str, Position] = {
            p.instrument.symbol: p for p in broker
        }

        all_symbols = set(local_map) | set(broker_map)
        drifts: list[DriftItem] = []

        for symbol in sorted(all_symbols):
            lp = local_map.get(symbol)
            bp = broker_map.get(symbol)
            local_qty = lp.quantity.value if lp else Decimal("0")
            broker_qty = bp.quantity.value if bp else Decimal("0")
            diff = local_qty - broker_qty
            if diff != Decimal("0"):
                drifts.append(
                    DriftItem(
                        symbol=symbol,
                        local_quantity=local_qty,
                        broker_quantity=broker_qty,
                        diff=diff,
                    )
                )
            # Also detect avg_price drift when quantities match
            if (
                lp is not None
                and bp is not None
                and local_qty == broker_qty
                and local_qty != Decimal("0")
            ):
                price_diff = abs(
                    lp.avg_price.value - bp.avg_price.value
                )
                if price_diff > Decimal("0.01"):
                    drifts.append(
                        DriftItem(
                            kind="position",
                            key=symbol,
                            severity=DriftSeverity.MEDIUM,
                            reason="avg_price drift",
                            local=lp.avg_price.value,
                            remote=bp.avg_price.value,
                        )
                    )

        return drifts

    # -- v3-ported comparison methods --------------------------------------

    def compare_orders(
        self,
        local: list[Order],
        broker: list[Order],
        *,
        price_tolerance: Decimal = Decimal("0.01"),
    ) -> list[DriftItem]:
        """Compare local vs broker orders and return drift items."""
        drifts: list[DriftItem] = []
        local_by = {_order_key(o): o for o in local}
        broker_by = {_order_key(o): o for o in broker}

        for key, bo in broker_by.items():
            lo = local_by.get(key)
            if lo is None:
                drifts.append(
                    DriftItem(
                        kind="order",
                        key=key,
                        severity=DriftSeverity.HIGH,
                        reason="missing local order",
                        local=None,
                        remote=bo,
                    )
                )
                continue
            drifts.extend(
                self._diff_order(lo, bo, key, price_tolerance),
            )

        for key, lo in local_by.items():
            if key not in broker_by:
                drifts.append(
                    DriftItem(
                        kind="order",
                        key=key,
                        severity=DriftSeverity.HIGH,
                        reason="missing broker order",
                        local=lo,
                        remote=None,
                    )
                )
        return drifts

    def compare_funds(
        self,
        local: Account,
        broker: Account,
        *,
        money_tolerance: Decimal = Decimal("0.01"),
    ) -> list[DriftItem]:
        """Compare local vs broker account funds and return drift items."""
        drifts: list[DriftItem] = []
        if (
            local.balance is not None
            and broker.balance is not None
            and abs(local.balance.amount - broker.balance.amount) > money_tolerance
        ):
            drifts.append(
                DriftItem(
                    kind="funds",
                    key=local.account_id.value,
                    severity=DriftSeverity.HIGH,
                    reason="balance mismatch",
                    local=local,
                    remote=broker,
                )
            )
        elif (
            local.equity is not None
            and broker.equity is not None
            and abs(local.equity.amount - broker.equity.amount) > money_tolerance
        ):
            drifts.append(
                DriftItem(
                    kind="funds",
                    key=local.account_id.value,
                    severity=DriftSeverity.MEDIUM,
                    reason="equity drift",
                    local=local,
                    remote=broker,
                )
            )
        return drifts

    # -- internal helpers --------------------------------------------------

    @staticmethod
    def _diff_order(
        local: Order,
        broker: Order,
        key: str,
        price_tolerance: Decimal,
    ) -> list[DriftItem]:
        out: list[DriftItem] = []
        if local.quantity.value != broker.quantity.value:
            out.append(
                DriftItem(
                    kind="order",
                    key=key,
                    severity=DriftSeverity.HIGH,
                    reason="quantity mismatch",
                    local=local,
                    remote=broker,
                )
            )
            return out
        if local.filled_quantity.value != broker.filled_quantity.value:
            out.append(
                DriftItem(
                    kind="order",
                    key=key,
                    severity=DriftSeverity.HIGH,
                    reason="filled_quantity mismatch",
                    local=local,
                    remote=broker,
                )
            )
            return out
        lp = local.price.value if local.price else None
        bp = broker.price.value if broker.price else None
        if lp is not None and bp is not None and abs(lp - bp) > price_tolerance:
            out.append(
                DriftItem(
                    kind="order",
                    key=key,
                    severity=DriftSeverity.MEDIUM,
                    reason="price drift",
                    local=local,
                    remote=broker,
                )
            )
        elif local.status is not broker.status:
            out.append(
                DriftItem(
                    kind="order",
                    key=key,
                    severity=DriftSeverity.LOW,
                    reason="status lag",
                    local=local,
                    remote=broker,
                )
            )
        return out


__all__ = ["DriftItem", "DriftSeverity", "ReconciliationEngine"]
