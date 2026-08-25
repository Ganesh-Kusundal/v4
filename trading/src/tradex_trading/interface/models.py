"""Pydantic response models for the HTTP API.

Pure data shapes — no behavior, no FastAPI coupling beyond ``BaseModel``.
Lifted out of ``fastapi_app`` so they can be imported by route modules,
tests, and the WebSocket message-shape documentation without pulling in
the whole app factory.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class HealthResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={"exclude_none": True})

    status: str
    check: str | None = None
    session_state: str | None = None


class PositionResponse(BaseModel):
    instrument: str
    quantity: str
    avg_price: str
    realized_pnl: str
    unrealized_pnl: str
    total_pnl: str
    is_long: bool
    is_short: bool


class AccountResponse(BaseModel):
    balance: str | None = None
    margin: str | None = None
    equity: str | None = None


class OrderResponse(BaseModel):
    order_id: str
    status: str
    message: str = ""


class ErrorResponse(BaseModel):
    error: str


__all__ = [
    "AccountResponse",
    "ErrorResponse",
    "HealthResponse",
    "OrderResponse",
    "PositionResponse",
]
