"""``/v1/option-chain`` and ``/v1/futures`` — derivatives chains."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/v1/option-chain/{underlying}")
async def option_chain(
    request: Request,
    underlying: str,
    expiry: str | None = None,
    live: bool = False,
    broker: str | None = None,
) -> dict[str, Any]:
    return await request.app.state.run(
        request.app.state.chains.option_chain,
        underlying,
        expiry,
        live=live,
        broker=broker,
    )


@router.get("/v1/futures/{underlying}")
async def futures(request: Request, underlying: str, broker: str | None = None) -> dict[str, Any]:
    return await request.app.state.run(request.app.state.chains.futures, underlying, broker)
