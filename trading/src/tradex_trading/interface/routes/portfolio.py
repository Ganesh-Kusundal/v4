"""``/positions``, ``/holdings`` — portfolio reads from the bound session."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from tradex_trading.interface._helpers import serialize_position
from tradex_trading.interface.models import PositionResponse
from tradex_trading.interface.routes.deps import get_session

router = APIRouter()


@router.get("/positions", response_model=list[PositionResponse])
async def get_positions(
    instrument: str | None = None,
    session: Any | None = Depends(get_session),
) -> list[PositionResponse]:
    if session is None:
        return []
    positions = session.portfolio.positions()
    result = [serialize_position(p) for p in positions]
    if instrument is not None:
        result = [p for p in result if instrument.lower() in p.instrument.lower()]
    return result


@router.get("/holdings", response_model=list)
async def get_holdings(session: Any | None = Depends(get_session)) -> list[dict]:
    """Get holdings (long-term positions)."""
    if session is None:
        raise HTTPException(status_code=400, detail="no session bound")
    try:
        holdings = session.portfolio.holdings() if hasattr(session.portfolio, "holdings") else []
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
