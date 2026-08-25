"""``/health`` and ``/health/{live,ready}`` — uptime and session readiness."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from tradex_trading.interface._helpers import is_ready, readiness
from tradex_trading.interface.models import HealthResponse
from tradex_trading.interface.routes.deps import get_session

router = APIRouter()


@router.get("/health", response_model=HealthResponse, response_model_exclude_none=True)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/health/live", response_model=HealthResponse, response_model_exclude_none=True)
async def health_live() -> HealthResponse:
    return HealthResponse(status="ok", check="live")


@router.get("/health/ready", response_model=HealthResponse, response_model_exclude_none=True)
async def health_ready(session: Any | None = Depends(get_session)) -> HealthResponse:
    s = readiness(session)
    if not is_ready(session):
        raise HTTPException(
            status_code=503,
            detail=f"session not ready: {s.session_state}",
        )
    return s


