"""``/extensions`` — broker capability flags (super-order, forever-order, etc.)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from tradex_domain.constants import (
    CAPABILITIES_KEY,
    CAPABILITY_NAMES,
    NO_SESSION_BOUND,
)

from tradex_interfaces.auth.deps import require_auth
from tradex_interfaces.routes.deps import get_session

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/extensions")
async def list_extensions(session: Any | None = Depends(get_session)) -> dict:
    """List broker capability flags."""
    if session is None:
        raise HTTPException(status_code=400, detail=NO_SESSION_BOUND)
    try:
        caps = getattr(session.broker, "capabilities", None)
        cap_map = {}
        if caps is not None:
            for name in CAPABILITY_NAMES:
                cap_map[name] = getattr(caps, name, False)
        return {CAPABILITIES_KEY: cap_map}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
