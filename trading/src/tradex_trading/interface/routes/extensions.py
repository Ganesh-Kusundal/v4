"""``/extensions`` — broker capability flags (super-order, forever-order, etc.)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from tradex_trading.interface.routes.deps import get_session

router = APIRouter()


@router.get("/extensions")
async def list_extensions(session: Any | None = Depends(get_session)) -> dict:
    """List broker capability flags."""
    if session is None:
        raise HTTPException(status_code=400, detail="no session bound")
    try:
        caps = getattr(session.broker, "capabilities", None)
        cap_map = {}
        if caps is not None:
            for name in (
                "supports_super_order",
                "supports_forever_order",
                "supports_slice_order",
                "supports_edis",
                "supports_kill_switch",
            ):
                cap_map[name] = getattr(caps, name, False)
        return {"capabilities": cap_map}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
