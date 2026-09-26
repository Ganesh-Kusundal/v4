"""``/positions``, ``/holdings`` — portfolio reads from the bound session."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException
from tradex_execution.projection import execution_projection

from tradex_interfaces._helpers import serialize_position
from tradex_interfaces.models import PositionResponse
from tradex_interfaces.routes.deps import get_session

router = APIRouter()


@router.get("/positions", response_model=list[PositionResponse])
async def get_positions(
    instrument: str | None = None,
    session: Any | None = Depends(get_session),
) -> list[PositionResponse]:
    if session is None:
        return []
    positions = session.engine.cache.all_positions()
    # The canonical economic values come from the same projection the parity
    # suite compares across modes, so this endpoint cannot drift from what
    # backtest/replay/live agree on. ``serialize_position`` still supplies the
    # shape and the mark fields, which are quote-derived and not part of the
    # economic projection.
    canonical_rows = cast(
        "list[dict[str, str]]", execution_projection([], positions)["positions"]
    )
    canonical = {row["instrument"]: row for row in canonical_rows}
    result = []
    for p in positions:
        row = serialize_position(p)
        econ = canonical.get(row.instrument)
        if econ is not None:
            row = row.model_copy(
                update={
                    "avg_price": econ["avg_price"],
                    "realized_pnl": econ["realized_pnl"],
                    "unrealized_pnl": econ["unrealized_pnl"],
                },
            )
        result.append(row)
    if instrument is not None:
        result = [p for p in result if instrument.lower() in p.instrument.lower()]
    return result


@router.get("/holdings", response_model=list)
async def get_holdings(session: Any | None = Depends(get_session)) -> list[dict]:
    """Get holdings (long-term positions)."""
    if session is None:
        raise HTTPException(status_code=400, detail="no session bound")
    try:
        holdings = list(session.broker.get_holdings())
        return [
            {
                "instrument_id": (
                    str(h.instrument.instrument_id)
                    if hasattr(h, "instrument")
                    else str(h)
                ),
                "quantity": float(h.quantity.value) if hasattr(h, "quantity") else 0,
            }
            for h in holdings
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
