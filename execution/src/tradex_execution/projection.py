"""Canonical projections for comparing execution modes."""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from tradex_domain.execution import Fill, Position


def _decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def execution_projection(
    fills: Iterable[Fill],
    positions: Iterable[Position],
    cash: Decimal | None = None,
) -> dict[str, object]:
    """Return the deterministic economic projection of execution state.

    The projection intentionally omits order ids, timestamps, and other mode
    specific metadata. Those values are not economic state and may differ
    between recorded, simulated, and live sources.
    """
    fill_rows = [
        {
            "instrument": str(fill.instrument.instrument_id),
            "side": fill.side.value,
            "quantity": _decimal(fill.quantity.value),
            "price": _decimal(fill.price.value),
        }
        for fill in fills
    ]
    position_rows = [
        {
            "instrument": str(position.instrument.instrument_id),
            "quantity": _decimal(position.quantity.value),
            "avg_price": _decimal(position.avg_price.value),
            "realized_pnl": _decimal(position.realized_pnl.amount),
            "unrealized_pnl": _decimal(position.unrealized_pnl.amount),
        }
        for position in sorted(positions, key=lambda item: str(item.instrument.instrument_id))
    ]
    projection: dict[str, object] = {"fills": fill_rows, "positions": position_rows}
    if cash is not None:
        projection["cash"] = _decimal(cash)
    return projection


__all__ = ["execution_projection"]
