"""``/positions``, ``/holdings`` — portfolio reads from the bound session."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException
from tradex_domain.constants import NO_SESSION_BOUND
from tradex_execution.projection import execution_projection

from tradex_interfaces._helpers import serialize_position
from tradex_interfaces.auth.deps import require_auth
from tradex_interfaces.models import PositionResponse
from tradex_interfaces.routes.deps import get_session, get_session_portfolio

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/positions", response_model=list[PositionResponse])
async def get_positions(
    instrument: str | None = None,
    session: Any | None = Depends(get_session),
) -> list[PositionResponse]:
    if session is None:
        return []
    # The live book: ``get_session_portfolio`` reads the OMS cache the
    # session's own fill path writes to, so a fill that lands while the
    # session is running is what this endpoint returns on the next request.
    portfolio = get_session_portfolio(session)
    positions = portfolio.positions
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
        raise HTTPException(status_code=400, detail=NO_SESSION_BOUND)
    # The same live book as ``/positions``. The broker's own holdings read is
    # deliberately NOT the source here: for paper the session's fill path
    # never writes the broker's position dict (``PaperFillSource`` owns no
    # projection, so the engine books the OMS cache), so a ``/holdings`` that
    # read ``broker.get_holdings()`` would report a flat account for a session
    # holding a filled position — the stub this replaces.
    portfolio = get_session_portfolio(session)
    return [
        {
            "instrument_id": str(position.instrument.instrument_id),
            "quantity": float(position.quantity.value),
        }
        for position in portfolio.positions
        if float(position.quantity.value) != 0.0
    ]
