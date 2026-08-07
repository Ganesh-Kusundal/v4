"""TradeX v4 HTTP API — FastAPI with CORS, OpenAPI, and WebSocket support."""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Security, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict
from tradex_domain.errors import CapabilityNotSupportedError

log = logging.getLogger(__name__)

#: Per-connection outbound queue bounds for /ws/stream. Producers (broker
#: threads via ``call_soon_threadsafe``) never await the socket; a single
#: writer task drains the queues, so a slow client cannot grow memory
#: unboundedly or stall the event loop.
#:
#: Ticks (quote/depth) are freshness-bound: on overflow the *oldest* queued
#: tick is dropped. Control messages (acks, fills) carry order events, so
#: they get a dedicated priority queue — the writer drains control first. A
#: control queue that is still full (client effectively gone) evicts its own
#: *oldest* message, so the newest order event always lands.
OUTBOUND_QUEUE_MAX = 1024
CONTROL_QUEUE_MAX = 256


def _enqueue_drop_oldest(
    queue: asyncio.Queue[dict[str, Any]], payload: dict[str, Any]
) -> int:
    """Enqueue *payload*, dropping the oldest queued message on overflow.

    Returns the number of messages dropped (0 normally).
    """
    try:
        queue.put_nowait(payload)
        return 0
    except asyncio.QueueFull:
        try:
            queue.get_nowait()  # drop the oldest queued message
            queue.put_nowait(payload)
            return 1
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            return 0


def _enqueue_control_drop_oldest(
    control: asyncio.Queue[dict[str, Any]], payload: dict[str, Any]
) -> int:
    """Enqueue a control message, dropping the oldest control on overflow.

    Returns the number of messages dropped (0 normally). Control messages
    (acks/fills) ride a small dedicated queue that the writer drains before
    ticks. When it is full (a client too slow to keep up), the *oldest*
    control is evicted — the newest order event always lands. Note a full
    control queue cannot be relieved by evicting ticks: the two queues have
    independent capacities, so overflow drops within the control queue.
    """
    try:
        control.put_nowait(payload)
        return 0
    except asyncio.QueueFull:
        try:
            control.get_nowait()  # drop the oldest queued control
            control.put_nowait(payload)
            return 1
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            return 0


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Auth header scheme (reusable across the module)
# ---------------------------------------------------------------------------

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(
    session: Any | None = None,
    api_key: str | None = None,
    outbound_max: int = OUTBOUND_QUEUE_MAX,
) -> FastAPI:
    """Create a FastAPI application backed by an optional TradingSession.

    ``outbound_max`` bounds each /ws/stream connection's outbound queue
    (drop-oldest on overflow) — small in tests to exercise backpressure.
    """
    app = FastAPI(title="TradeX v4 API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.session = session
    app.state.api_key = api_key
    # Per-app refcounting registry: shares the session's live MarketFeed
    # across every /ws/stream connection. None for paper/no-feed sessions.
    app.state.feed_registry = None
    if session is not None:
        from tradex_trading.runtime.market_feed import FeedRegistry, MarketFeed

        feed = getattr(session, "market_feed", None)
        if isinstance(feed, MarketFeed):
            app.state.feed_registry = FeedRegistry(feed)
    # Single source of truth for depth-mode normalization (shared with
    # MarketFeed/FeedRegistry) — imported once, not per WebSocket connection.
    from tradex_trading.runtime.market_feed import normalize_depth

    # -- Auth dependency (closure over *app*) ----------------------------------

    async def verify_api_key(
        api_key: str | None = Security(api_key_header),
    ) -> None:
        expected = app.state.api_key
        if expected is None:
            return  # No auth configured
        if api_key != expected:
            raise HTTPException(status_code=403, detail="Invalid API key")

    # --- Health ----------------------------------------------------------------

    @app.get("/health", response_model=HealthResponse, response_model_exclude_none=True)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/health/live", response_model=HealthResponse, response_model_exclude_none=True)
    async def health_live() -> HealthResponse:
        return HealthResponse(status="ok", check="live")

    @app.get("/health/ready", response_model=HealthResponse, response_model_exclude_none=True)
    async def health_ready() -> HealthResponse:
        ready = _readiness(app.state.session)
        if not _is_ready(app.state.session):
            raise HTTPException(
                status_code=503,
                detail=f"session not ready: {ready.session_state}",
            )
        return ready

    # --- Positions -------------------------------------------------------------

    @app.get("/positions", response_model=list[PositionResponse])
    async def get_positions(instrument: str | None = None) -> list[PositionResponse]:
        s = app.state.session
        if s is None:
            return []
        positions = s.portfolio.positions()
        result = [_serialize_position(p) for p in positions]
        if instrument is not None:
            result = [p for p in result if instrument.lower() in p.instrument.lower()]
        return result

    # --- Holdings --------------------------------------------------------------

    @app.get("/holdings", response_model=list)
    async def get_holdings() -> list[dict]:
        """Get holdings (long-term positions)."""
        s = app.state.session
        if s is None:
            raise HTTPException(status_code=400, detail="no session bound")
        try:
            holdings = s.portfolio.holdings() if hasattr(s.portfolio, 'holdings') else []
            return [
                {
                    "instrument_id": (
                        str(h.instrument.instrument_id)
                        if hasattr(h, "instrument")
                        else str(h)
                    ),
                    "quantity": float(h.quantity.value) if hasattr(h, 'quantity') else 0,
                }
                for h in holdings
            ]
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

    # --- Orders ----------------------------------------------------------------

    @app.get("/orders", response_model=list[OrderResponse])
    async def list_orders(
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[OrderResponse]:
        s = app.state.session
        if s is None:
            return []
        orders = (
            s.trade.get_orderbook() if hasattr(s.trade, "get_orderbook") else []
        )
        result = [
            OrderResponse(order_id=str(o.order_id), status=str(o.status))
            for o in orders
        ]
        if status is not None:
            result = [o for o in result if o.status.lower() == status.lower()]
        return result[offset : offset + limit]

    @app.get("/orders/{order_id}", response_model=OrderResponse)
    async def get_order(order_id: str) -> OrderResponse:
        s = app.state.session
        if s is None:
            raise HTTPException(status_code=404, detail="No session bound")
        try:
            order = s.trade.get_order(order_id)
            return OrderResponse(order_id=str(order.order_id), status=str(order.status))
        except Exception as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.post("/orders", response_model=OrderResponse, dependencies=[Depends(verify_api_key)])
    async def place_order(body: dict) -> OrderResponse:
        s = app.state.session
        if s is None:
            raise HTTPException(status_code=503, detail="no session bound")
        try:
            from tradex_domain.enums import OrderSide, OrderType, TimeInForce
            from tradex_domain.execution import OrderRequest
            from tradex_domain.instruments import Equity
            from tradex_domain.value_objects import Price, Quantity

            # Resolve instrument
            instrument_id = body.get("instrument_id")
            if instrument_id:
                parts = instrument_id.split(":", 1)
                if len(parts) == 2:
                    instrument = Equity.of(parts[0], parts[1])
                else:
                    raise HTTPException(
                        status_code=422,
                        detail="instrument_id must be in 'EXCHANGE:SYMBOL' format",
                    )
            else:
                exchange = body.get("exchange")
                symbol = body.get("symbol")
                if not exchange or not symbol:
                    raise HTTPException(
                        status_code=422,
                        detail="provide either 'instrument_id' or both 'exchange' and 'symbol'",
                    )
                instrument = Equity.of(exchange, symbol)

            # Build OrderRequest
            side = OrderSide(body["side"])
            order_type = OrderType(body.get("order_type", "MARKET"))
            quantity = Quantity(Decimal(str(body["quantity"])))
            price = Price(Decimal(str(body["price"]))) if body.get("price") is not None else None
            time_in_force = (
                TimeInForce(body["time_in_force"])
                if body.get("time_in_force")
                else TimeInForce.DAY
            )

            request = OrderRequest(
                instrument=instrument,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price,
                time_in_force=time_in_force,
            )

            receipt = s.trade.submit(request)
            return OrderResponse(
                order_id=receipt.order_id.value,
                status=str(receipt.status),
                message=receipt.message,
            )
        except HTTPException:
            raise
        except KeyError as exc:
            raise HTTPException(status_code=422, detail=f"missing required field: {exc}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            log.exception("order submission failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.put(
        "/orders/{order_id}",
        response_model=OrderResponse,
        dependencies=[Depends(verify_api_key)],
    )
    async def modify_order(order_id: str, body: dict) -> OrderResponse:
        s = app.state.session
        if s is None:
            raise HTTPException(status_code=400, detail="no session bound")
        try:
            order = s.trade.modify(order_id, body)
            return OrderResponse(order_id=str(order.order_id), status="modified")
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.delete(
        "/orders/{order_id}",
        response_model=OrderResponse,
        dependencies=[Depends(verify_api_key)],
    )
    async def cancel_order(order_id: str) -> OrderResponse:
        s = app.state.session
        if s is None:
            raise HTTPException(status_code=400, detail="no session bound")
        try:
            order = s.trade.cancel(order_id)
            return OrderResponse(order_id=str(order.order_id), status="cancelled")
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    # --- Market data -----------------------------------------------------------

    @app.get("/quotes/{exchange}:{symbol}", response_model=dict[str, Any])
    async def get_quote(exchange: str, symbol: str) -> dict[str, Any]:
        s = app.state.session
        if s is None:
            raise HTTPException(status_code=404, detail="No session bound")
        try:
            quote = s.market.quote(exchange=exchange, symbol=symbol)
            return dict(quote) if not isinstance(quote, dict) else quote
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

    @app.get("/search", response_model=list[str])
    async def search_instruments(q: str) -> list[str]:
        s = app.state.session
        if s is None:
            return []
        try:
            results = s.market.search(q)
            return list(results)
        except Exception:
            return []

    @app.get("/history/{instrument_id}", response_model=list)
    async def get_history(
        instrument_id: str,
        timeframe: str = "1d",
        limit: int = 100,
    ) -> list[dict]:
        """Get historical bars for an instrument."""
        s = app.state.session
        if s is None:
            raise HTTPException(status_code=400, detail="no session bound")
        try:
            from tradex_domain.enums import Timeframe
            from tradex_domain.value_objects import InstrumentId

            iid = InstrumentId.parse(instrument_id)
            tf = Timeframe(timeframe)
            history = s.market.history(iid, tf)
            bars = []
            for bar in history:
                bars.append({
                    "timestamp": str(bar.timestamp),
                    "open": float(bar.ohlc.open.value),
                    "high": float(bar.ohlc.high.value),
                    "low": float(bar.ohlc.low.value),
                    "close": float(bar.ohlc.close.value),
                    "volume": float(bar.volume.value) if bar.volume else 0,
                })
            return bars[:limit]
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

    @app.get("/option-chain/{underlying}")
    async def get_option_chain(
        underlying: str,
        expiry: str | None = None,
        live: bool = False,
    ) -> dict:
        """Option chain for an underlying.

        ``underlying`` accepts ``EXCHANGE:SYMBOL`` (e.g. ``MCX:GOLD``), a
        registry alias/key, or a bare symbol resolved from the loaded master.
        NFO/BFO chains come from the live REST endpoint (OI/volume/greeks);
        MCX and other non-NFO exchanges are derived from the instrument master.
        An optional ``expiry`` (YYYY-MM-DD) filters to a single expiry.
        ``live=true`` enriches the nearest expiry's strikes with real-time
        LTP / OI / volume (best-effort batch quotes for the ATM region).
        """
        s = app.state.session
        if s is None:
            raise HTTPException(status_code=400, detail="no session bound")
        try:
            inst = _resolve_underlying_instrument(s, underlying)
            chain = s.market.option_chain(inst, expiry)
            if live:
                return _enrich_chain_live(s, chain)
            return _serialize_option_chain(chain)
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

    @app.get("/future-chain/{underlying}")
    async def get_future_chain(underlying: str) -> dict:
        """Future contracts on an underlying, derived from the loaded master."""
        s = app.state.session
        if s is None:
            raise HTTPException(status_code=400, detail="no session bound")
        try:
            inst = _resolve_underlying_instrument(s, underlying)
            futures = s.market.future_chain(inst)
            return {
                "underlying": str(inst.instrument_id),
                "futures": [
                    {
                        "instrument": str(f.instrument_id),
                        "symbol": f.symbol,
                        "expiry": f.expiry.isoformat() if getattr(f, "expiry", None) else None,
                    }
                    for f in futures
                ],
            }
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

    # --- Account ---------------------------------------------------------------

    @app.get("/account", response_model=AccountResponse)
    async def get_account() -> AccountResponse:
        s = app.state.session
        if s is None:
            raise HTTPException(status_code=404, detail="no session bound")
        acct = s.portfolio.account()
        return AccountResponse(
            balance=str(acct.balance),
            margin=str(acct.margin),
            equity=str(acct.equity),
        )

    # --- Extensions ------------------------------------------------------------

    @app.get("/extensions")
    async def list_extensions() -> dict:
        """List available broker extensions."""
        s = app.state.session
        if s is None:
            raise HTTPException(status_code=400, detail="no session bound")
        try:
            ext = s.extension
            available = ext.list_available() if hasattr(ext, 'list_available') else []
            caps = ext.capabilities() if hasattr(ext, 'capabilities') else {}
            return {"available": [str(a) for a in available], "capabilities": caps}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

    # --- WebSocket (ReactiveBus bridge) ----------------------------------------

    @app.websocket("/ws/stream")
    async def ws_stream(ws: WebSocket) -> None:
        s = app.state.session
        if s is None:
            await ws.close(code=1011, reason="no session bound")
            return

        await ws.accept()
        disposables: list[Any] = []
        loop = asyncio.get_running_loop()
        registry = app.state.feed_registry
        if registry is not None:
            registry.acquire()
        # Per-connection wanted set: instrument id -> Instrument (None if the
        # session has no live feed, e.g. paper mode — subscriptions still ack).
        wanted: dict[Any, Any] = {}
        depth_mode: dict[Any, str] = {}
        # Bounded outbound queues + single writer task: producers enqueue
        # (never await the socket) so a slow consumer cannot buffer the
        # event loop's memory without bound. Control messages (acks/fills)
        # get strict priority over ticks; on overflow the *oldest* control
        # is dropped — freshness wins for market data, newest control wins
        # for order events.
        ticks: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=outbound_max)
        control: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=CONTROL_QUEUE_MAX)
        dropped: list[int] = [0]  # closure cell — incremented by producers

        async def _writer() -> None:
            """Drain control first, then ticks; exit when the socket closes."""
            while True:
                try:
                    payload = control.get_nowait()
                except asyncio.QueueEmpty:
                    tick_get = asyncio.create_task(ticks.get())
                    ctrl_get = asyncio.create_task(control.get())
                    try:
                        done, _pending = await asyncio.wait(
                            {tick_get, ctrl_get},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        payload = done.pop().result()
                    finally:
                        for fut in (tick_get, ctrl_get):
                            if not fut.done():
                                fut.cancel()
                try:
                    await ws.send_json(payload)
                except Exception:  # socket closed; receive loop cleans up
                    return

        writer_task = asyncio.create_task(_writer())

        try:
            from tradex_brokers.common.provider_common import instrument_from_id
            from tradex_domain.events import OrderFilled
            from tradex_domain.market import Depth, Quote
            from tradex_domain.value_objects import InstrumentId

            bus = s.bus

            def _send_quote(quote: Quote) -> None:
                if quote.instrument.instrument_id not in wanted:
                    return
                dropped[0] += _enqueue_drop_oldest(ticks, {
                    "type": "quote",
                    "instrument": str(quote.instrument.instrument_id),
                    "ltp": str(quote.ltp.value),
                    "bid": str(quote.bid.value) if quote.bid is not None else None,
                    "ask": str(quote.ask.value) if quote.ask is not None else None,
                })

            def _send_depth(depth: Depth) -> None:
                iid = depth.instrument.instrument_id
                if iid not in wanted or depth_mode.get(iid) == "off":
                    return
                dropped[0] += _enqueue_drop_oldest(ticks, {
                    "type": "depth",
                    "instrument": str(iid),
                    "bids": [
                        [str(price.value), str(qty.value)]
                        for price, qty in depth.bids
                    ],
                    "asks": [
                        [str(price.value), str(qty.value)]
                        for price, qty in depth.asks
                    ],
                    "levels": len(depth.bids) + len(depth.asks),
                })

            def _send_fill(event: OrderFilled) -> None:
                fill = event.fill
                dropped[0] += _enqueue_control_drop_oldest(control, {
                    "type": "fill",
                    "order_id": str(fill.order_id),
                    "instrument": str(fill.instrument.instrument_id),
                    "price": str(fill.price.value),
                    "quantity": str(fill.quantity.value),
                })

            def _send_order(payload: Any) -> None:
                """Forward a broker order-update onto the control (priority) queue.

                Accepts either a domain ``Order`` (wired broker backend) or an
                ``OrderPlaced`` bus event (backend-less fallback) — the event
                is unwrapped to its ``.order``. Order events ride the same
                dedicated control queue as acks/fills, so they are delivered
                ahead of market ticks and never evicted by them.
                """
                order = getattr(payload, "order", payload)
                dropped[0] += _enqueue_control_drop_oldest(control, {
                    "type": "order",
                    "order_id": str(order.order_id),
                    "status": str(getattr(order, "status", "")),
                    "instrument": str(order.instrument.instrument_id),
                    "side": str(getattr(order, "side", "")),
                    "quantity": (
                        str(order.quantity.value)
                        if getattr(order, "quantity", None) is not None
                        else None
                    ),
                    "price": str(order.price.value) if getattr(order, "price", None) else None,
                })

            def _ack(message: dict[str, Any]) -> None:
                dropped[0] += _enqueue_control_drop_oldest(control, message)

            def _parse_instruments(raw: object) -> list[Any]:
                if not isinstance(raw, list) or not raw:
                    raise ValueError("instruments must be a non-empty list")
                instruments: list[Any] = []
                for item in raw:
                    instruments.append(instrument_from_id(InstrumentId.parse(str(item))))
                return instruments

            async def _subscribe(instruments: list[Any], depth: str) -> None:
                """Subscribe a connection; roll back on cap errors."""
                for inst in instruments:
                    iid = inst.instrument_id
                    wanted[iid] = inst
                    depth_mode[iid] = depth
                try:
                    if registry is not None:
                        registry.subscribe(instruments, depth=depth)
                except (ValueError, CapabilityNotSupportedError) as exc:
                    # Cap exceeded, or depth requested on an exchange with no
                    # depth feed (NSE only): roll back the connection's local
                    # state and surface a loud error instead of silently
                    # dropping keys or killing the socket.
                    for inst in instruments:
                        wanted.pop(inst.instrument_id, None)
                        depth_mode.pop(inst.instrument_id, None)
                    _ack({"type": "error", "message": str(exc)})
                    return
                feed_info: dict[str, Any] = {}
                if registry is not None and registry.feed is not None:
                    feed_info = {
                        "depth_levels": registry.feed.depth_levels,
                        "max_stream_instruments": registry.feed.max_stream_instruments,
                    }
                feed_info["dropped"] = dropped[0]
                _ack({
                    "type": "subscribed",
                    "instruments": [str(i.instrument_id) for i in instruments],
                    "depth": depth,
                    "feed": feed_info,
                })

            async def _handle_message(text: str) -> None:
                try:
                    msg = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    _ack({"type": "error", "message": "invalid JSON"})
                    return
                if not isinstance(msg, dict):
                    _ack({"type": "error", "message": "message must be an object"})
                    return
                msg_type = msg.get("type")
                if msg_type == "subscribe":
                    try:
                        instruments = _parse_instruments(msg.get("instruments"))
                    except ValueError as exc:
                        _ack({"type": "error", "message": str(exc)})
                        return
                    depth = normalize_depth(msg.get("depth"))
                    await _subscribe(instruments, depth)
                    if msg.get("snapshot") is True:
                        await _snapshot(instruments)
                elif msg_type == "unsubscribe":
                    try:
                        instruments = _parse_instruments(msg.get("instruments"))
                    except ValueError as exc:
                        _ack({"type": "error", "message": str(exc)})
                        return
                    for inst in instruments:
                        wanted.pop(inst.instrument_id, None)
                        depth_mode.pop(inst.instrument_id, None)
                    if registry is not None:
                        registry.unsubscribe(instruments)
                    _ack({
                        "type": "unsubscribed",
                        "instruments": [str(i.instrument_id) for i in instruments],
                    })
                elif msg_type == "subscribe_orders":
                    await _subscribe_orders()
                elif msg_type == "unsubscribe_orders":
                    _unsubscribe_orders()
                    _ack({"type": "unsubscribed_orders"})
                elif msg_type == "stats":
                    # Surface backpressure stats so a client can observe how
                    # many ticks were dropped under overload (drop counter).
                    _ack({
                        "type": "stats",
                        "dropped": dropped[0],
                        "queued_ticks": ticks.qsize(),
                        "queued_control": control.qsize(),
                    })
                else:
                    _ack({"type": "error", "message": f"unknown message type: {msg_type!r}"})

            # Optional broker order-stream subscription: when the session has
            # a wired order backend (live Dhan/Upstox), a client can opt in to
            # receive broker order-updates as control-priority ``order``
            # messages. The handle is cancelled on unsubscribe and on socket
            # teardown.
            order_handle: list[Any] = []

            def _on_order(order: Any) -> None:
                loop.call_soon_threadsafe(_send_order, order)

            async def _subscribe_orders() -> None:
                if order_handle:
                    _ack({"type": "subscribed_orders"})
                    return
                try:
                    handle = s.stream.subscribe_orders(_on_order)
                except Exception as exc:  # noqa: BLE001 – no backend / not READY
                    _ack({"type": "error", "message": f"order stream unavailable: {exc}"})
                    return
                order_handle.append(handle)
                _ack({"type": "subscribed_orders"})

            def _unsubscribe_orders() -> None:
                if not order_handle:
                    return
                handle = order_handle.pop()
                try:
                    handle.cancel()
                except Exception:  # noqa: BLE001 – best-effort teardown
                    pass

            async def _snapshot(instruments: list[Any]) -> None:
                """Push one-shot REST quotes (and depth when requested).

                Opt-in (``"snapshot": true``) because a large set would hammer
                the provider's REST endpoint. Fetches run concurrently; a
                slow/failed fetch for one instrument never blocks the rest.
                """
                try:
                    market = s.market
                except Exception:  # noqa: BLE001 — session not READY (paper)
                    return

                def _fetch(inst: Any) -> None:
                    iid = inst.instrument_id
                    try:
                        quote = market.quote(inst)
                    except Exception:  # noqa: BLE001 — best-effort snapshot
                        quote = None
                    if quote is not None and iid in wanted:
                        loop.call_soon_threadsafe(_send_quote, quote)
                    if depth_mode.get(iid, "off") != "off":
                        try:
                            depth = market.depth(inst)
                        except Exception:  # noqa: BLE001 — best-effort snapshot
                            return
                        if iid in wanted:
                            loop.call_soon_threadsafe(_send_depth, depth)

                await asyncio.gather(*(asyncio.to_thread(_fetch, inst) for inst in instruments))

            def _on_quote(quote: Quote) -> None:
                loop.call_soon_threadsafe(_send_quote, quote)

            def _on_depth(depth: Depth) -> None:
                loop.call_soon_threadsafe(_send_depth, depth)

            def _on_fill(event: OrderFilled) -> None:
                loop.call_soon_threadsafe(_send_fill, event)

            d_quote = bus.of_type(Quote).subscribe(_on_quote)
            d_depth = bus.of_type(Depth).subscribe(_on_depth)
            d_fill = bus.of_type(OrderFilled).subscribe(_on_fill)
            disposables.extend([d_quote, d_depth, d_fill])

            while True:
                text = await ws.receive_text()
                await _handle_message(text)
        except WebSocketDisconnect:
            pass
        finally:
            # Cancel and drain the writer so no "task was destroyed but it is
            # pending" warning leaks on loop teardown (it may be parked in
            # ``outbound.get()`` when the client leaves).
            writer_task.cancel()
            try:
                await writer_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 – teardown
                pass
            if registry is not None:
                try:
                    if wanted:
                        registry.unsubscribe(list(wanted.values()))
                except Exception:  # pragma: no cover – defensive
                    pass
                try:
                    registry.release()  # last client stops the shared feed
                except Exception:  # pragma: no cover – defensive
                    pass
            # Cancel any live broker order-stream subscription for this client.
            _unsubscribe_orders()
            for d in disposables:
                try:
                    d.dispose()
                except Exception:  # pragma: no cover – defensive
                    pass

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_underlying_instrument(session: Any, raw: str) -> Any:
    """Resolve a user-provided underlying string to a domain Instrument.

    Accepts ``EXCHANGE:SYMBOL`` (e.g. ``MCX:GOLD``), a registry alias/key,
    or a bare symbol found by searching the loaded master. Raises
    ``LookupError`` when nothing resolves.
    """
    from tradex_brokers.common.provider_common import instrument_from_id
    from tradex_domain.value_objects import InstrumentId

    stripped = raw.strip()
    if "::" not in stripped and ":" in stripped:
        try:
            return instrument_from_id(InstrumentId.parse(stripped))
        except ValueError:
            pass
    broker = getattr(session, "_broker", None)
    registry = getattr(broker, "registry", None)
    if registry is not None:
        iid = registry.resolve(stripped)
        if iid is not None:
            return instrument_from_id(iid)
    try:
        results = list(session.market.search(stripped))
    except Exception:  # noqa: BLE001 — search is best-effort resolution
        results = []
    for inst in results:
        iid = getattr(inst, "instrument_id", None)
        if iid is not None and iid.underlying.upper() == stripped.upper():
            return inst
    raise LookupError(f"unknown underlying instrument: {stripped!r}")


def _serialize_option_chain(chain: Any) -> dict:
    """Serialize an OptionChain into a JSON-friendly dict."""
    expiries = []
    for exp in chain.expiries():
        expiries.append(
            {
                "expiry": exp.expiry_date.isoformat(),
                "reference_price": (
                    str(exp.reference_price.value) if exp.reference_price is not None else None
                ),
                "pairs": [
                    {
                        "strike": str(p.strike.value),
                        "call": str(p.call.instrument_id),
                        "put": str(p.put.instrument_id),
                    }
                    for p in exp.pairs
                ],
            }
        )
    return {"underlying": str(chain.underlying.instrument_id), "expiries": expiries}


def _enrich_chain_live(session: Any, chain: Any, max_strikes: int = 11) -> dict:
    """Attach real-time LTP / OI / volume / greeks to the nearest expiry's strikes.

    Best-effort: batch-quotes the ATM-centred strike window (nearest expiry
    first) and attaches ``call_live``/``put_live`` legs per pair. Any quote
    failure degrades to the static chain rather than erroring the request.

    MCX (and other non-NFO/BFO/IDX) underlyings have no REST batch-quote
    endpoint, so OI/volume enrichment is opted out — only the ATM LTP window
    is kept, fetched per-leg and best-effort. Greeks ride in ``quote.metadata``
    when the provider feed carries them (Upstox WS ``option_greeks``).
    """
    expiries = chain.expiries()
    if not expiries:
        return {"underlying": str(chain.underlying.instrument_id), "live": True, "expiries": []}
    underlying_exchange = str(getattr(chain.underlying.exchange, "value", "")).upper()
    batch_supported = underlying_exchange in {"NFO", "BFO", "IDX"}
    target = min(expiries, key=lambda exp: exp.expiry_date)
    reference = target.reference_price.value if target.reference_price is not None else None
    if reference is not None:
        ordered = sorted(target.pairs, key=lambda p: abs(p.strike.value - reference))
    else:
        ordered = list(target.pairs)
    chosen = ordered[:max_strikes]
    instruments = [inst for p in chosen for inst in (p.call, p.put)]
    quotes: dict[Any, Any] = {}
    if batch_supported:
        try:
            quotes = session.market.quote_batch(instruments)
        except Exception:  # noqa: BLE001 — live enrichment is best-effort
            quotes = {}
    by_id = {str(iid): quote for iid, quote in quotes.items()}
    # ATM-window ids only — the per-leg LTP fallback below must never fan out
    # over the whole chain (MCX has ~1800 pairs; that would be thousands of
    # REST calls).
    chosen_ids = {str(i.instrument_id) for i in instruments}

    def _leg(instrument_id: str, inst: Any) -> dict | None:
        if instrument_id not in chosen_ids:
            return None
        quote = by_id.get(instrument_id)
        if quote is None and not batch_supported:
            # MCX: no REST batch quotes — fetch LTP per-leg (ATM window only).
            try:
                quote = session.market.ltp(inst)
            except Exception:  # noqa: BLE001 — best-effort LTP
                return None
            return {"ltp": str(quote.value)}
        if quote is None:
            return None
        metadata = quote.metadata or {}
        greeks = metadata.get("greeks")
        return {
            "ltp": str(quote.ltp.value),
            "oi": str(quote.open_interest.value) if quote.open_interest is not None else None,
            "volume": str(quote.volume.value) if quote.volume is not None else None,
            "greeks": dict(greeks) if isinstance(greeks, dict) else None,
        }

    out_expiries = []
    for exp in expiries:
        out_expiries.append(
            {
                "expiry": exp.expiry_date.isoformat(),
                "reference_price": (
                    str(exp.reference_price.value) if exp.reference_price is not None else None
                ),
                "pairs": [
                    {
                        "strike": str(p.strike.value),
                        "call": str(p.call.instrument_id),
                        "put": str(p.put.instrument_id),
                        "call_live": _leg(str(p.call.instrument_id), p.call),
                        "put_live": _leg(str(p.put.instrument_id), p.put),
                    }
                    for p in exp.pairs
                ],
            }
        )
    return {
        "underlying": str(chain.underlying.instrument_id),
        "live": True,
        "expiries": out_expiries,
    }


def _serialize_position(pos: Any) -> PositionResponse:
    """Serialize a Position domain object to a PositionResponse."""
    return PositionResponse(
        instrument=str(pos.instrument.instrument_id),
        quantity=str(pos.quantity.value),
        avg_price=str(pos.avg_price.value),
        realized_pnl=str(pos.realized_pnl.amount),
        unrealized_pnl=str(pos.unrealized_pnl.amount),
        total_pnl=str(pos.total_pnl.amount),
        is_long=pos.is_long,
        is_short=pos.is_short,
    )


def _readiness(session: Any) -> HealthResponse:
    """Readiness facts — single source of truth for /health/ready and the
    pre-bind probe in :func:`start_fastapi_server`.

    ``None`` session (no session bound) counts as ready, mirroring the
    pre-existing no-session behaviour of the route.
    """
    if session is None:
        return HealthResponse(status="ok", check="ready")
    return HealthResponse(status="ok", check="ready", session_state=str(session.state))


def _is_ready(session: Any) -> bool:
    """True when the bound session (if any) is READY — the /health/ready gate."""
    if session is None:
        return True
    return str(getattr(session, "state", None)) == "READY"


#: Serve-spec environment keys — how the importable ASGI factory below learns
#: how to rebuild a session inside a uvicorn-spawned subprocess (workers /
#: reload). Environment variables are inherited across the spawn boundary;
#: an in-memory TradingSession object is not (it holds threads/sockets).
_SERVE_ENV_BROKER = "TRADEX_SERVE_BROKER"
_SERVE_ENV_API_KEY = "TRADEX_SERVE_API_KEY"


def serve_app() -> FastAPI:
    """Importable ASGI app factory used for uvicorn ``workers`` / ``reload``.

    uvicorn >= 0.51 starts worker/reload subprocesses with the ``spawn``
    multiprocessing context, which pickles the uvicorn Config — an in-memory
    ``TradingSession`` (threads, sockets, locks) cannot cross that boundary,
    and a re-imported module cannot see the parent process's objects either.
    So the serve spec (broker + optional API key) travels in the environment
    (inherited by spawned children) and each process rebuilds its own READY
    session here.
    """
    import os

    from tradex_domain import BrokerId

    from tradex_trading.sdk.session import TradingSession

    broker = os.environ.get(_SERVE_ENV_BROKER, "PAPER").upper()
    api_key = os.environ.get(_SERVE_ENV_API_KEY) or None
    if broker == "PAPER":
        session = TradingSession.paper()
    else:
        session = TradingSession.live(BrokerId(broker), confirm=True)
    return create_app(session, api_key=api_key)


def start_fastapi_server(
    session: Any = None,
    host: str = "127.0.0.1",
    port: int = 8080,
    api_key: str | None = None,
    workers: int = 1,
    reload: bool = False,
) -> None:
    """Start a uvicorn server with the FastAPI app.

    Runs a pre-bind readiness probe (the same check /health/ready performs)
    and refuses to bind when the session is present but not READY. ``workers``
    and ``reload`` are forwarded to uvicorn; either one switches to the
    importable :func:`serve_app` factory because uvicorn spawns subprocesses
    for them (an in-memory session cannot be pickled into the child).
    """
    import os

    import uvicorn

    if workers < 1:
        raise ValueError(f"workers must be >= 1, got {workers}")

    broker_id = str(
        getattr(session, "broker_id", "PAPER") if session is not None else "PAPER"
    ).upper()
    if (workers > 1 or reload) and broker_id != "PAPER":
        # Every spawned worker/reload process rebuilds its own session via
        # serve_app(), which for a live broker means a fresh token/auth flow
        # per process (interactive TOTP, cooldowns, concurrent logins).
        raise ValueError(
            f"--workers/--reload require a paper session (got broker={broker_id}); "
            "live brokers re-authenticate per worker process, so scale with "
            "separate `tradex serve` instances on different ports instead"
        )

    # Pre-bind readiness probe — mirrors /health/ready. A raw (NEW) session
    # must not start accepting traffic.
    ready = _readiness(session)
    if not _is_ready(session):
        raise ValueError(
            f"session not ready (state={ready.session_state}); "
            "call session.start() before serve"
        )

    if workers > 1 or reload:
        # uvicorn 0.51 spawns workers/reload subprocesses and pickles the
        # Config — carry the spec in the env (inherited by spawn children)
        # and let each process build its own session via serve_app().
        os.environ[_SERVE_ENV_BROKER] = broker_id
        if api_key is not None:
            os.environ[_SERVE_ENV_API_KEY] = api_key
        else:
            os.environ.pop(_SERVE_ENV_API_KEY, None)
        uvicorn.run(
            "tradex_trading.interface.fastapi_app:serve_app",
            factory=True,
            host=host,
            port=port,
            workers=workers,
            reload=reload,
        )
        return

    # Single-process path: no serve spec needed — leave the environment clean
    # so a later factory-mode serve in this process cannot read a stale spec.
    os.environ.pop(_SERVE_ENV_BROKER, None)
    os.environ.pop(_SERVE_ENV_API_KEY, None)
    app = create_app(session, api_key=api_key)
    uvicorn.run(app, host=host, port=port)
