"""Typed wire DTOs for the TradeX v4 HTTP API (spec 0003 tranche 1).

Pure data shapes — Pydantic models with ``extra="forbid"`` so unknown keys
are rejected at the boundary. No business logic; route handlers map these
onto the domain/application layer.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlaceOrderRequest(_Strict):
    instrument_id: str | None = None
    exchange: str | None = None
    symbol: str | None = None
    side: str
    order_type: str = "MARKET"
    quantity: Any
    price: Any = None
    trigger_price: Any = None
    time_in_force: str | None = None
    product: str | None = None


class BracketOrderRequestDto(PlaceOrderRequest):
    stop_loss_price: Any = None
    target_price: Any = None
    trailing_jump: Any = None


class OrderUpdateRequest(_Strict):
    instrument_id: str | None = None
    exchange: str | None = None
    symbol: str | None = None
    side: str | None = None
    order_type: str | None = None
    quantity: Any = None
    price: Any = None
    trigger_price: Any = None
    time_in_force: str | None = None
    product: str | None = None
    stop_loss_price: Any = None
    target_price: Any = None
    trailing_jump: Any = None


class OrderMutationResponse(BaseModel):
    order_id: str
    status: str
    message: str = ""


class OrdersListResponse(BaseModel):
    orders: list[dict[str, Any]] = []


class BacktestRequest(_Strict):
    exchange: str = "NSE"
    symbol: str
    interval: str = "D"
    from_utc: int | None = None
    to_utc: int | None = None
    strategy: str = "sma_cross"
    params: dict[str, Any] | None = None
    initial_capital: Any = None


class ScannerRunRequest(_Strict):
    id: str
    window_days: int = 30


class BacktestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metrics: Any = None
    equity_curve: list[Any] = []
    trades: list[Any] = []


__all__ = [
    "BacktestRequest",
    "BacktestResponse",
    "BracketOrderRequestDto",
    "OrderMutationResponse",
    "OrderUpdateRequest",
    "OrdersListResponse",
    "PlaceOrderRequest",
    "ScannerRunRequest",
]


def to_order_request(body: Any, session: Any, correlation_id: Any) -> Any:
    """Thin pass-through: route handlers keep building domain requests.

    Kept as a documented seam per spec 0003; currently unused because the
    existing ``_build_order_request`` already owns that mapping. Exposed so
    contracts consumers have a single named entry point if they want it.
    """
    return body
