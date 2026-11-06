"""``/v1/history`` — candle history for one instrument and in batch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query, Request

from tradex_gateway.models import HistoryBatchRequest
from tradex_gateway.routes.deps import _query_moment

router = APIRouter()


@router.get("/v1/history/{instrument_id}")
async def history(
    request: Request,
    instrument_id: str,
    timeframe: str = "1m",
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    source: str = "auto",
    broker: str | None = None,
    fill_gaps: bool = False,
) -> dict[str, Any]:
    end = _query_moment(to) if to else datetime.now(UTC)
    start = _query_moment(from_) if from_ else end - timedelta(days=5)
    return await request.app.state.run(
        request.app.state.history.history,
        instrument_id,
        timeframe,
        start,
        end,
        source=source,
        broker=broker,
        fill_gaps=fill_gaps,
    )


@router.post("/v1/history/batch")
async def history_batch(request: Request, payload: HistoryBatchRequest) -> list[dict[str, Any]]:
    requests = [item.model_dump() for item in payload.requests]
    return await request.app.state.run(
        request.app.state.history.batch, requests, fill_gaps=payload.fill_gaps
    )
