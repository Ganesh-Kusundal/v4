"""``/v1/quote`` and ``/v1/quotes`` — latest quote snapshots."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from tradex_gateway.models import QuotesRequest

router = APIRouter()


@router.get("/v1/quote/{instrument_id}")
async def quote(request: Request, instrument_id: str, broker: str | None = None) -> dict[str, Any]:
    return await request.app.state.run(request.app.state.quotes.quote, instrument_id, broker)


@router.post("/v1/quotes")
async def quotes(
    request: Request, payload: QuotesRequest, broker: str | None = None
) -> dict[str, Any]:
    return await request.app.state.run(request.app.state.quotes.quotes, payload.instruments, broker)
