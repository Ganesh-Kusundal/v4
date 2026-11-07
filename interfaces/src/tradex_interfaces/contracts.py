"""Typed wire DTOs for the TradeX v4 HTTP API (spec 0003 tranche 1).

Pure data shapes — Pydantic models with ``extra="forbid"`` so unknown keys
are rejected at the boundary. No business logic; route handlers map these
onto the domain/application layer.

The chart data plane's bodies are the deliberate exception on the ``extra``
axis: those endpoints predate this module and read their bodies with
``.get``, so they have always *tolerated* unknown keys. Tightening them would
silently break the openalgo-charts frontend, so they opt into
``extra="ignore"`` individually and say so in their docstrings — a documented
wire-compat choice, not a default.
"""

from __future__ import annotations

from decimal import InvalidOperation
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _to_decimal(value: Any) -> Any:
    """Coerce a wire money value to ``Decimal``, or raise ``ValueError``.

    ``Decimal(str(value))`` raises ``decimal.InvalidOperation`` for anything
    that is not decimal-shaped (a mapping, a list, ``True``, ``"abc"``), and
    ``InvalidOperation`` is an ``ArithmeticError``, *not* a ``ValueError`` —
    so the route's ``except ValueError`` never saw it and the request
    answered 500 instead of 422. Raising ``ValueError`` here converts the
    failure at the boundary, where Pydantic turns it into a 422
    ``validation_error`` envelope before any handler runs.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"expected a decimal number, got {value!r}") from exc


#: Required money field (quantity, price, ...). ``None`` is rejected by the
#: ``Decimal`` type even though the validator lets it through.
_Money = Annotated[Decimal, BeforeValidator(_to_decimal)]

#: Optional money field (``price``, ``trigger_price``, protective legs).
_MoneyOpt = Annotated[Decimal | None, BeforeValidator(_to_decimal)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlaceOrderRequest(_Strict):
    instrument_id: str | None = None
    exchange: str | None = None
    symbol: str | None = None
    side: str
    order_type: str = "MARKET"
    quantity: _Money
    price: _MoneyOpt = None
    trigger_price: _MoneyOpt = None
    time_in_force: str | None = None
    product: str | None = None


class BracketOrderRequestDto(PlaceOrderRequest):
    stop_loss_price: _MoneyOpt = None
    target_price: _MoneyOpt = None
    trailing_jump: _MoneyOpt = None


class OrderUpdateRequest(_Strict):
    instrument_id: str | None = None
    exchange: str | None = None
    symbol: str | None = None
    side: str | None = None
    order_type: str | None = None
    quantity: _MoneyOpt = None
    price: _MoneyOpt = None
    trigger_price: _MoneyOpt = None
    time_in_force: str | None = None
    product: str | None = None
    stop_loss_price: _MoneyOpt = None
    target_price: _MoneyOpt = None
    trailing_jump: _MoneyOpt = None


class OrderMutationResponse(BaseModel):
    order_id: str
    status: str
    message: str = ""


class BacktestRequest(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    exchange: str = "NSE"
    symbol: str
    interval: str = "D"
    from_utc: int | None = Field(default=None, alias="from")
    to_utc: int | None = Field(default=None, alias="to")
    strategy: str = "sma_cross"
    params: dict[str, Any] | None = None
    initial_capital: _MoneyOpt = None
    fees: Any = None  # accepted for backward compat, ignored (costs are always on)


class ScannerRunRequest(_Strict):
    id: str
    window_days: int = 30


class BacktestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metrics: Any = None
    equity_curve: list[Any] = []
    trades: list[Any] = []


# ---------------------------------------------------------------------------
# Chart data plane request bodies (spec 0003 tranche 1)
#
# ``extra="ignore"``: these three handlers have always read their bodies with
# ``.get`` and ignored anything they did not name, so unknown keys were — and
# must remain — tolerated. Every field below is one the old code actually
# read, with the same default it used.
# ---------------------------------------------------------------------------


class IndicatorComputeRequest(BaseModel):
    """POST /api/charts/indicators/compute — ``{exchange, symbol, interval,
    from?, to?, id, params?}``.

    ``id`` stays required because the old handler answered 422 without it;
    ``interval`` defaults to ``"5m"`` and ``exchange`` to ``"NSE"`` exactly as
    the ``.get(key, default)`` reads did.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    exchange: str = "NSE"
    symbol: str = ""
    interval: str = "5m"
    from_utc: int | None = Field(default=None, alias="from")
    to_utc: int | None = Field(default=None, alias="to")
    params: dict[str, Any] | None = None


class SeriesComputeRequest(BaseModel):
    """POST /api/charts/transforms/{id} and /profiles/{id} —
    ``{id?, params?, bars: [{time, open, high, low, close, volume}]}``.

    ``bars`` is required (the old ``body.get("bars") or []`` rejected a
    missing one with 422 too); the handler keeps its own non-empty check so
    the endpoint-specific message survives. An ``id`` key in the body is
    ignored — the path parameter is the one that dispatches, as before.
    """

    model_config = ConfigDict(extra="ignore")

    bars: list[dict[str, Any]]
    params: dict[str, Any] | None = None


class SeasonalityRequest(BaseModel):
    """POST /api/charts/seasonality — ``{exchange, symbol, interval, from?,
    to?, params?}``; the query-string ``startYear``/``cutoffPercent``/...
    overrides ride the route's own Query parameters."""

    model_config = ConfigDict(extra="ignore")

    exchange: str = "NSE"
    symbol: str = ""
    interval: str = "D"
    from_utc: int | None = Field(default=None, alias="from")
    to_utc: int | None = Field(default=None, alias="to")
    params: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Chart data plane response envelopes
#
# The envelope (top-level keys) is the contract; row/leaf payloads are
# carried as ``dict[str, Any]`` because they are shaped by the frontend and
# by the domain objects the route maps, not by this module. Each model is
# ``extra="forbid"`` so a key appearing or disappearing in a handler is a
# failure here rather than a silent wire change.
# ---------------------------------------------------------------------------


class SymbolsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[dict[str, Any]]
    source: str
    universe: str | None = None


class HistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    exchange: str
    interval: str
    source: str
    timeframe_resampled: str
    last_closed_time: int | None
    bars: list[dict[str, Any]]


class IndicatorCatalogueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    indicators: list[dict[str, Any]]


class IndicatorComputeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    points: list[dict[str, Any]]
    meta: dict[str, Any]


class SeriesTransformResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    bars: list[dict[str, Any]]


class ProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    result: Any


class SeasonalityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table: dict[str, Any]
    meta: dict[str, Any]


class BookResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orders: list[dict[str, Any]]
    positions: list[dict[str, Any]]


class ChartAccountResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    balance: str
    margin: str
    unrealized_pnl: str
    positions: list[dict[str, Any]]


class StrategiesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategies: list[dict[str, Any]]
    scanners: list[dict[str, Any]]
    backtestable: list[dict[str, Any]]


class SafetyWireResponse(BaseModel):
    """``/api/charts/safety`` — the six keys ``SafetyReport.to_wire`` emits."""

    model_config = ConfigDict(extra="forbid")

    feed: dict[str, Any]
    unknown_orders: list[dict[str, Any]]
    reconciliation: dict[str, Any]
    strategies: list[dict[str, Any]]
    boot_refused: bool
    reconciliation_checked: bool


class ScannerRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scanner: str
    window_days: int
    results: list[dict[str, Any]]


class WorkspaceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layout_id: str
    revision: int
    updated_at: str


class WorkspaceDetail(WorkspaceSummary):
    data: Any = None


class WorkspacePutResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layout_id: str
    revision: int
    updated_at: str


class HoldingRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instrument_id: str
    quantity: float


class ExtensionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capabilities: dict[str, Any]


__all__ = [
    "BacktestRequest",
    "BacktestResponse",
    "BookResponse",
    "BracketOrderRequestDto",
    "ChartAccountResponse",
    "ExtensionsResponse",
    "HistoryResponse",
    "HoldingRow",
    "IndicatorCatalogueResponse",
    "IndicatorComputeRequest",
    "IndicatorComputeResponse",
    "OrderMutationResponse",
    "OrderUpdateRequest",
    "PlaceOrderRequest",
    "ProfileResponse",
    "SafetyWireResponse",
    "ScannerRunRequest",
    "ScannerRunResponse",
    "SeasonalityRequest",
    "SeasonalityResponse",
    "SeriesComputeRequest",
    "SeriesTransformResponse",
    "StrategiesResponse",
    "SymbolsResponse",
    "WorkspaceDetail",
    "WorkspacePutResponse",
    "WorkspaceSummary",
]
