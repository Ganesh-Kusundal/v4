"""Pydantic response models for the HTTP API.

Pure data shapes — no behavior, no FastAPI coupling beyond ``BaseModel``.
Lifted out of ``fastapi_app`` so they can be imported by route modules,
tests, and the WebSocket message-shape documentation without pulling in
the whole app factory.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

# Re-export typed wire DTOs so callers can import them from either home.
from tradex_interfaces.contracts import (  # noqa: F401
    BacktestRequest,
    BacktestResponse,
    BracketOrderRequestDto,
    FutureChainRequest,
    FutureChainResponse,
    FutureContract,
    HistoryBar,
    HistoryRequest,
    HistoryResponse,
    OptionChainRequest,
    OptionChainResponse,
    OrderMutationResponse,
    OrderUpdateRequest,
    PlaceOrderRequest,
    QuoteRequest,
    QuoteResponse,
    ScannerRunRequest,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
)


class HealthResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={"exclude_none": True})

    status: str
    check: str | None = None
    session_state: str | None = None
    feed_state: str | None = None
    feed_ready: bool | None = None


class PositionResponse(BaseModel):
    instrument: str
    quantity: str
    avg_price: str
    realized_pnl: str
    unrealized_pnl: str
    total_pnl: str
    is_long: bool
    is_short: bool
    mark_price: str | None = None
    marked_at: str | None = None
    mark_source: str | None = None
    mark_stale: bool = False


class AccountResponse(BaseModel):
    balance: str | None = None
    margin: str | None = None
    equity: str | None = None


class OrderResponse(BaseModel):
    order_id: str
    status: str
    message: str = ""


#: Every machine-readable ``error.code`` this app can emit — the closed set
#: the G13 envelope is defined over. ``code`` is what clients branch on, so a
#: new code has to be added here deliberately (and typed-checked at every
#: construction site) rather than appearing as an untracked string. The list
#: mirrors ``_STATUS_CODE_MAP`` and the special-cased branches in
#: ``fastapi_app``'s HTTPException handler, plus ``"error"``, the fallback
#: for a status that has no mapping.
ErrorCode = Literal[
    "auth_invalid",
    "auth_missing",
    "conflict",
    "csrf_missing",
    "error",
    "forbidden",
    "internal",
    "live_orders_disabled",
    "no_session",  # 503 when no TradingSession is bound (NO_SESSION_BOUND)
    "not_found",
    "rate_limited",
    "stale_revision",
    "validation_error",
]


class ErrorDetail(BaseModel):
    """Stable machine-readable error envelope (G13)."""

    code: ErrorCode
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


__all__ = [
    "AccountResponse",
    "ErrorDetail",
    "ErrorCode",
    "ErrorResponse",
    "FutureChainRequest",
    "FutureChainResponse",
    "FutureContract",
    "HealthResponse",
    "HistoryBar",
    "HistoryRequest",
    "HistoryResponse",
    "OptionChainRequest",
    "OptionChainResponse",
    "OrderResponse",
    "PositionResponse",
    "QuoteRequest",
    "QuoteResponse",
    "SearchRequest",
    "SearchResponse",
    "SearchResultItem",
]
