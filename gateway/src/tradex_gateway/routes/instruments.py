"""``/v1/instruments`` — instrument search and cross-broker resolution."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from tradex_gateway.directory import InstrumentDirectory

router = APIRouter()


@router.get("/v1/instruments/search")
async def search(
    request: Request,
    q: str,
    asset_class: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    return await request.app.state.run(
        request.app.state.directory.search,
        q,
        asset_class=asset_class,
        limit=max(1, min(limit, 200)),
    )


@router.get("/v1/instruments/resolve")
async def resolve(
    request: Request,
    q: str,
    asset_class: str | None = None,
) -> dict[str, Any]:
    resolved = await request.app.state.run(request.app.state.directory.resolve, q, asset_class)
    return {
        **InstrumentDirectory.instrument_meta(
            resolved.instrument, resolved.brokers, resolved.lot_sizes
        ),
        "per_broker": {
            name: str(instrument.instrument_id) if instrument is not None else None
            for name, instrument in resolved.per_broker.items()
        },
    }
