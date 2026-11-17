"""``/v1/expired/*`` — expired F&O contracts and their historical candles."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Query, Request

from tradex_gateway.errors import BadRequest

router = APIRouter()


def _query_date(value: str | None, name: str) -> date:
    if not value:
        raise BadRequest(f"{name} is required (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise BadRequest(f"invalid {name}: {value!r}") from exc


@router.get("/v1/expired/options/{underlying}")
async def expired_options(
    request: Request,
    underlying: str,
    expiry: str,
    broker: str | None = None,
) -> dict[str, Any]:
    return await request.app.state.run(
        request.app.state.expired.option_contracts, underlying, expiry, broker
    )


@router.get("/v1/expired/futures/{underlying}")
async def expired_futures(
    request: Request,
    underlying: str,
    expiry: str,
    broker: str | None = None,
) -> dict[str, Any]:
    return await request.app.state.run(
        request.app.state.expired.future_contracts, underlying, expiry, broker
    )


@router.get("/v1/expired/candles")
async def expired_candles(
    request: Request,
    key: str,
    interval: str = "day",
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    broker: str | None = None,
) -> dict[str, Any]:
    start = _query_date(from_, "from")
    end = _query_date(to, "to")
    return await request.app.state.run(
        request.app.state.expired.candles, key, interval, start, end, broker
    )
