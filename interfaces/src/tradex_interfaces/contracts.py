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


# ---------------------------------------------------------------------------
# Market data request/response DTOs
#
# These models type the wire contract for /quotes, /search, /history,
# /option-chain, and /future-chain endpoints. They follow the same pattern
# as the rest of this module: strict validation with ``extra="forbid"``.
# ---------------------------------------------------------------------------


class QuoteRequest(_Strict):
    """GET /quotes/{exchange}:{symbol} — path parameters only."""

    exchange: str
    symbol: str


class QuoteResponse(BaseModel):
    """Real-time quote envelope."""

    model_config = ConfigDict(extra="ignore")

    instrument_id: str | None = None
    symbol: str | None = None
    exchange: str | None = None
    last_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    volume: float | None = None
    timestamp: str | None = None


class SearchRequest(_Strict):
    """GET /search?q=..."""

    q: str


class SearchResultItem(BaseModel):
    """Single search result row."""

    model_config = ConfigDict(extra="forbid")

    instrument_id: str
    symbol: str
    exchange: str
    asset_class: str


SearchResponse = list[SearchResultItem]


class HistoryRequest(_Strict):
    """GET /history/{instrument_id}?timeframe=&limit="""

    instrument_id: str
    timeframe: str = "1d"
    limit: int = 100


class HistoryBar(BaseModel):
    """Single OHLCV bar."""

    model_config = ConfigDict(extra="forbid")

    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float


HistoryResponse = list[HistoryBar]


class OptionChainRequest(_Strict):
    """GET /option-chain/{underlying}?expiry=&live="""

    underlying: str
    expiry: str | None = None
    live: bool = False


class OptionChainResponse(BaseModel):
    """Option chain envelope — payload shape varies by broker/exchange."""

    model_config = ConfigDict(extra="ignore")

    underlying: str | None = None
    expiry: str | None = None
    strikes: list[dict[str, Any]] | None = None
    enriched: bool = False


class FutureChainRequest(_Strict):
    """GET /future-chain/{underlying}"""

    underlying: str


class FutureContract(BaseModel):
    """Single future contract row."""

    model_config = ConfigDict(extra="forbid")

    instrument: str
    symbol: str
    expiry: str | None = None


class FutureChainResponse(BaseModel):
    """Future chain envelope."""

    model_config = ConfigDict(extra="forbid")

    underlying: str
    futures: list[FutureContract]


# ---------------------------------------------------------------------------
# WebSocket stream message DTOs (spec 0003 tranche 1)
#
# These model the JSON messages exchanged over /ws/stream. Each handler in
# stream.py validates incoming messages against these shapes before acting on
# them, so malformed payloads are rejected with a structured error rather than
# causing an unhandled exception mid-handler.
# ---------------------------------------------------------------------------


class WSSubscribeRequest(_Strict):
    """``{type: "subscribe", instruments: [...], depth: str, snapshot?: bool}``"""

    type: str = "subscribe"
    instruments: list[str]
    depth: str = "off"
    snapshot: bool = False


class WSUnsubscribeRequest(_Strict):
    """``{type: "unsubscribe", instruments: [...]}``"""

    type: str = "unsubscribe"
    instruments: list[str]


class WSSubscribeBarsRequest(_Strict):
    """``{type: "subscribe_bars", bars: [{instrument, interval, ...}]}``"""

    type: str = "subscribe_bars"
    bars: list[dict[str, Any]] | None = None
    instruments: list[dict[str, Any]] | None = None


class WSUnsubscribeBarsRequest(_Strict):
    """``{type: "unsubscribe_bars", bars: [...]}``"""

    type: str = "unsubscribe_bars"
    bars: list[dict[str, Any] | str]


class WSIndicatorSubRequest(_Strict):
    """``{type: "indicator-sub", indicators: [{instrument, interval, ids, mode?}]}``"""

    type: str = "indicator-sub"
    indicators: list[dict[str, Any]]


class WSIndicatorUnsubRequest(_Strict):
    """``{type: "indicator-unsub", indicators: [{instrument, interval, ids?}]}``"""

    type: str = "indicator-unsub"
    indicators: list[dict[str, Any]]


class WSReplayStartRequest(_Strict):
    """``{type: "replay-start", instrument, minutes?, start_time?, speed?}``"""

    type: str = "replay-start"
    instrument: str
    minutes: int | None = None
    start_time: str | None = None
    speed: float | None = None


class WSReplaySeekRequest(_Strict):
    """``{type: "replay-seek", timestamp}``"""

    type: str = "replay-seek"
    timestamp: int


class WSReplaySpeedRequest(_Strict):
    """``{type: "replay-speed", speed}``"""

    type: str = "replay-speed"
    speed: float


# ---------------------------------------------------------------------------
# WebSocket stream response/frame DTOs (spec 0003 tranche 2)
#
# Models for every frame type the /ws/stream endpoint emits to clients.
# These complement the request models above and complete the bidirectional
# wire contract. The stream handler builds frames by constructing these
# models rather than ad hoc dicts, so field renames are type errors in
# Python instead of silent wire changes.
# ---------------------------------------------------------------------------


class WSSubscribeOrdersRequest(_Strict):
    """Client -> server: opt into broker order updates."""

    type: str = "subscribe_orders"


class WSUnsubscribeOrdersRequest(_Strict):
    """Client -> server: stop receiving broker order updates."""

    type: str = "unsubscribe_orders"


class WSReplayPauseRequest(_Strict):
    """Client -> server: pause a running replay."""

    type: str = "replay-pause"


class WSReplayResumeRequest(_Strict):
    """Client -> server: resume a paused replay."""

    type: str = "replay-resume"


class WSReplayStepRequest(_Strict):
    """Client -> server: step one bar in replay."""

    type: str = "replay-step"


class WSReplayStopRequest(_Strict):
    """Client -> server: stop a replay run."""

    type: str = "replay-stop"


class WSStatsRequest(_Strict):
    """Client -> server: request queue stats."""

    type: str = "stats"


class WSFeedStatusRequest(_Strict):
    """Client -> server: request feed status."""

    type: str = "feed_status"


class WSQuoteFrame(BaseModel):
    """Server -> client: live quote update."""

    model_config = ConfigDict(extra="forbid")

    type: str = "quote"
    instrument: str
    ltp: str
    bid: str | None = None
    ask: str | None = None


class WSDepthLevel(BaseModel):
    """One level in a depth book."""

    model_config = ConfigDict(extra="forbid")

    price: float
    qty: float


class WSDepthFrame(BaseModel):
    """Server -> client: depth book snapshot."""

    model_config = ConfigDict(extra="forbid")

    type: str = "depth"
    instrument: str
    bids: list[WSDepthLevel]
    asks: list[WSDepthLevel]
    ltp: float


class WSBarFrame(BaseModel):
    """Server -> client: aggregated bar (live or replay)."""

    model_config = ConfigDict(extra="forbid")

    type: str = "bar"
    instrument: str
    interval: str
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool
    source: str = "live"
    run_id: str | None = None
    provisional: bool = False


class WSFillFrame(BaseModel):
    """Server -> client: order fill notification."""

    model_config = ConfigDict(extra="forbid")

    type: str = "fill"
    order_id: str
    instrument: str
    price: str
    quantity: str


class WSPositionFrame(BaseModel):
    """Server -> client: position update."""

    model_config = ConfigDict(extra="forbid")

    type: str = "position"
    instrument: str
    quantity: str
    avg_price: str
    realized_pnl: str
    unrealized_pnl: str
    mark_price: str | None = None
    marked_at: str | None = None
    mark_source: str | None = None


class WSOrderFrame(BaseModel):
    """Server -> client: order status update."""

    model_config = ConfigDict(extra="forbid")

    type: str = "order"
    order_id: str
    status: str
    instrument: str
    side: str
    quantity: str | None = None
    price: str | None = None


class WSSubscribedAck(BaseModel):
    """Server -> client: subscription acknowledged."""

    model_config = ConfigDict(extra="forbid")

    type: str = "subscribed"
    instruments: list[str]
    depth: str
    feed: dict[str, Any]


class WSUnsubscribedAck(BaseModel):
    """Server -> client: unsubscription acknowledged."""

    model_config = ConfigDict(extra="forbid")

    type: str = "unsubscribed"
    instruments: list[str]


class WSSubscribedOrdersAck(BaseModel):
    """Server -> client: order subscription acknowledged."""

    model_config = ConfigDict(extra="forbid")

    type: str = "subscribed_orders"


class WSUnsubscribedOrdersAck(BaseModel):
    """Server -> client: order unsubscription acknowledged."""

    model_config = ConfigDict(extra="forbid")

    type: str = "unsubscribed_orders"


class WSSubscribedBarsAck(BaseModel):
    """Server -> client: bar subscription acknowledged."""

    model_config = ConfigDict(extra="forbid")

    type: str = "subscribed_bars"
    bars: list[str]
    active: int


class WSUnsubscribedBarsAck(BaseModel):
    """Server -> client: bar unsubscription acknowledged."""

    model_config = ConfigDict(extra="forbid")

    type: str = "unsubscribed_bars"


class WSIndicatorAck(BaseModel):
    """Server -> client: indicator subscription acknowledged."""

    model_config = ConfigDict(extra="forbid")

    type: str = "indicator-ack"
    id: str


class WSReplayStartedFrame(BaseModel):
    """Server -> client: replay started."""

    model_config = ConfigDict(extra="forbid")

    type: str = "replay-started"
    run_id: str
    instrument: str
    interval: str
    total_bars: int


class WSReplayStoppedFrame(BaseModel):
    """Server -> client: replay stopped."""

    model_config = ConfigDict(extra="forbid")

    type: str = "replay-stopped"
    run_id: str


class WSReplayPausedFrame(BaseModel):
    """Server -> client: replay paused."""

    model_config = ConfigDict(extra="forbid")

    type: str = "replay-paused"
    run_id: str


class WSReplayResumedFrame(BaseModel):
    """Server -> client: replay resumed."""

    model_config = ConfigDict(extra="forbid")

    type: str = "replay-resumed"
    run_id: str


class WSReplaySteppedFrame(BaseModel):
    """Server -> client: replay stepped one bar."""

    model_config = ConfigDict(extra="forbid")

    type: str = "replay-stepped"
    run_id: str
    index: int


class WSReplayErrorFrame(BaseModel):
    """Server -> client: replay error."""

    model_config = ConfigDict(extra="forbid")

    type: str = "replay-error"
    message: str
    run_id: str | None = None


class WSFeedStatusFrame(BaseModel):
    """Server -> client: feed health status."""

    model_config = ConfigDict(extra="forbid")

    type: str = "feed_status"
    state: str
    ready: bool
    generation: int
    last_event_at: str | None = None
    age: float | None = None
    live_orders_enabled: bool


class WSQueueStatsFrame(BaseModel):
    """Server -> client: outbound queue statistics."""

    model_config = ConfigDict(extra="forbid")

    type: str = "queue_stats"
    dropped: int
    queued_ticks: int
    queued_control: int
    queued_barrier: int
    tick_cap: int
    control_cap: int
    resync_required: bool


class WSErrorFrame(BaseModel):
    """Server -> client: generic error."""

    model_config = ConfigDict(extra="forbid")

    type: str = "error"
    message: str


__all__ = [
    "BacktestRequest",
    "BacktestResponse",
    "BookResponse",
    "BracketOrderRequestDto",
    "ChartAccountResponse",
    "ExtensionsResponse",
    "FutureChainRequest",
    "FutureChainResponse",
    "FutureContract",
    "HistoryBar",
    "HistoryRequest",
    "HistoryResponse",
    "HoldingRow",
    "IndicatorCatalogueResponse",
    "IndicatorComputeRequest",
    "IndicatorComputeResponse",
    "OptionChainRequest",
    "OptionChainResponse",
    "OrderMutationResponse",
    "OrderUpdateRequest",
    "PlaceOrderRequest",
    "ProfileResponse",
    "QuoteRequest",
    "QuoteResponse",
    "SafetyWireResponse",
    "ScannerRunRequest",
    "ScannerRunResponse",
    "SearchRequest",
    "SearchResponse",
    "SearchResultItem",
    "SeasonalityRequest",
    "SeasonalityResponse",
    "SeriesComputeRequest",
    "SeriesTransformResponse",
    "StrategiesResponse",
    "SymbolsResponse",
    "WSBarFrame",
    "WSDepthFrame",
    "WSDepthLevel",
    "WSErrorFrame",
    "WSFeedStatusFrame",
    "WSFeedStatusRequest",
    "WSFillFrame",
    "WSIndicatorAck",
    "WSIndicatorSubRequest",
    "WSIndicatorUnsubRequest",
    "WSOrderFrame",
    "WSPositionFrame",
    "WSQueueStatsFrame",
    "WSQuoteFrame",
    "WSReplayErrorFrame",
    "WSReplayPauseRequest",
    "WSReplayPausedFrame",
    "WSReplayResumeRequest",
    "WSReplayResumedFrame",
    "WSReplaySeekRequest",
    "WSReplaySpeedRequest",
    "WSReplayStartRequest",
    "WSReplayStartedFrame",
    "WSReplayStepRequest",
    "WSReplaySteppedFrame",
    "WSReplayStopRequest",
    "WSReplayStoppedFrame",
    "WSSubscribeBarsRequest",
    "WSSubscribeOrdersRequest",
    "WSSubscribeRequest",
    "WSSubscribedAck",
    "WSSubscribedBarsAck",
    "WSSubscribedOrdersAck",
    "WSStatsRequest",
    "WSUnsubscribeBarsRequest",
    "WSUnsubscribeOrdersRequest",
    "WSUnsubscribeRequest",
    "WSUnsubscribedAck",
    "WSUnsubscribedBarsAck",
    "WSUnsubscribedOrdersAck",
    "WorkspaceDetail",
    "WorkspacePutResponse",
    "WorkspaceSummary",
]
