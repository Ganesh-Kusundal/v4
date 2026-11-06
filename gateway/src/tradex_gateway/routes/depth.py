"""``/v1/depth`` — order-book depth."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from tradex_gateway.errors import BadRequest

router = APIRouter()


@router.get("/v1/depth/{instrument_id}")
async def depth(
    request: Request,
    instrument_id: str,
    levels: int = 5,
    broker: str | None = None,
) -> dict[str, Any]:
    if levels < 1 or levels > 30:
        raise BadRequest("levels must be between 1 and 30")
    return await request.app.state.run(request.app.state.depth.depth, instrument_id, levels, broker)
