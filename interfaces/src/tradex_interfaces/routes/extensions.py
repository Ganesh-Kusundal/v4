"""``/extensions`` — broker capability flags (super-order, forever-order, etc.)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from tradex_domain.constants import (
    CAPABILITIES_KEY,
    CAPABILITY_NAMES,
)

from tradex_interfaces.routes.deps import require_session

router = APIRouter()


@router.get("/extensions")
async def list_extensions(session: Any = Depends(require_session(400))) -> Any:
    """List broker capability flags."""
    try:
        caps = getattr(session.broker, "capabilities", None)
        cap_map = {}
        if caps is not None:
            for name in CAPABILITY_NAMES:
                cap_map[name] = getattr(caps, name, False)
        return {CAPABILITIES_KEY: cap_map}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
