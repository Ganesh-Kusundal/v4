"""TradeX v4 HTTP API — FastAPI with CORS, OpenAPI, and WebSocket support."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from tradex_domain.errors import CapabilityNotSupportedError

# Pydantic response models, WebSocket queue helpers, and the API-key auth
# dependency now live in sibling modules. They are re-exported from here so
# existing callers and tests that import them off ``fastapi_app`` keep
# working; new code should import directly from the sibling modules.
from tradex_trading.interface.auth import api_key_header, make_verify_api_key
from tradex_trading.interface.models import (  # noqa: F401  (re-exported)
    AccountResponse,
    ErrorResponse,
    HealthResponse,
    OrderResponse,
    PositionResponse,
)
from tradex_trading.interface.queueing import (  # noqa: F401  (re-exported)
    CONTROL_QUEUE_MAX,
    OUTBOUND_QUEUE_MAX,
    _enqueue_control_drop_oldest,
    _enqueue_drop_oldest,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

#: Built frontend assets (``frontend/dist``), resolved relative to the repo
#: root two levels up from this package's ``interface/`` directory. The mount
#: is opt-in by presence: no dist directory means API-only behavior, exactly
#: as before, so a source checkout without a built frontend serves nothing
#: extra and tests never depend on a node build having run.
_UI_DIST_DIR = (
    Path(__file__).resolve().parents[4] / "frontend" / "dist"
)


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
    # Chart data plane (closed-bar history, indicator compute, strategy
    # backtests): one router the openalgo-charts frontend is built against.
    from tradex_trading.interface.routes.chart import create_chart_router

    app.include_router(create_chart_router(session))
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

    # -- Auth dependency (built once per app via the auth module) -----------

    verify_api_key = make_verify_api_key(lambda: app.state)

    # -- HTTP routes (each module owns its slice) ----------------------------
    # Stream route is intentionally NOT included — the WebSocket closure
    # depends on app.state.feed_registry + normalize_depth + outbound_max
    # and stays in this function below.
    from tradex_trading.interface.routes import (
        account as _account_route,
        extensions as _extensions_route,
        health as _health_route,
        market_data as _market_data_route,
        orders as _orders_route,
        portfolio as _portfolio_route,
    )

    for _r in (
        _health_route.router,
        _portfolio_route.router,
        _orders_route.router,
        _market_data_route.router,
        _account_route.router,
        _extensions_route.router,
    ):
        app.include_router(_r)

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
            """Drain control first, then ticks; exit when the socket closes.

            Regression guard: with ``asyncio.wait(FIRST_COMPLETED)`` over two
            non-empty queues, BOTH getter tasks complete at once. Popping a
            single ``done`` future discarded the other's already-dequeued
            message — observed as lost ``replay_stopped`` acks under a bar
            flood, and able to drop order/fill controls during any tick
            storm. Consume every completed getter and send control first.
            """
            async def _get_tagged(
                q: asyncio.Queue[dict[str, Any]], priority: int
            ) -> tuple[int, dict[str, Any]]:
                return (priority, await q.get())

            while True:
                batch: list[tuple[int, dict[str, Any]]] = []
                # Strict priority for anything already queued: control first.
                try:
                    while True:
                        batch.append((0, control.get_nowait()))
                except asyncio.QueueEmpty:
                    pass
                try:
                    while True:
                        batch.append((1, ticks.get_nowait()))
                except asyncio.QueueEmpty:
                    pass
                if not batch:
                    t_get = asyncio.create_task(_get_tagged(ticks, 1))
                    c_get = asyncio.create_task(_get_tagged(control, 0))
                    try:
                        done, _pending = await asyncio.wait(
                            {t_get, c_get},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for fut in done:
                            batch.append(fut.result())
                    finally:
                        for fut in (t_get, c_get):
                            if not fut.done():
                                fut.cancel()
                batch.sort(key=lambda item: item[0])
                for _, payload in batch:
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
                elif msg_type == "subscribe_bars":
                    await _subscribe_bars(msg)
                elif msg_type == "unsubscribe_bars":
                    _unsubscribe_bars(msg)
                    _ack({"type": "unsubscribed_bars"})
                elif msg_type == "replay_start":
                    await _replay_start(msg)
                elif msg_type == "replay_pause":
                    await _replay_pause()
                elif msg_type == "replay_resume":
                    await _replay_resume()
                elif msg_type == "replay_speed":
                    await _replay_speed(msg)
                elif msg_type == "replay_stop":
                    await _replay_stop()
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

            # --- Bar streaming (chart forming bars + replay) ----------------
            #
            # One BarAggregator per (instrument, interval) per connection.
            # Quotes reach the aggregator through a bus subscription filtered
            # by the bar wanted-set; frames ride the ticks queue so a slow
            # chart client drops old forming bars rather than stalling. The
            # same aggregation path serves live quotes and (Task: replay)
            # synthetic ones — the source only changes what gets published.
            bar_aggregators: dict[tuple[str, str], Any] = {}
            bar_disposables: list[Any] = []
            replay_state: dict[str, Any] = {"task": None}

            def _send_bar_frame(frame: Any) -> None:
                dropped[0] += _enqueue_drop_oldest(ticks, {
                    "type": "bar",
                    "instrument": frame.instrument,
                    "interval": frame.timeframe,
                    "time": frame.time,
                    "open": frame.open,
                    "high": frame.high,
                    "low": frame.low,
                    "close": frame.close,
                    "volume": frame.volume,
                    "closed": frame.closed,
                    "source": getattr(frame, "source", "live"),
                })

            def _make_aggregator(inst_id: str, interval: str) -> Any:
                from tradex_domain.enums import Timeframe

                from tradex_trading.runtime.bar_aggregator import BarAggregator

                return BarAggregator(
                    inst_id,
                    Timeframe(interval),
                    on_frame=_send_bar_frame,
                )

            def _on_bar_quote(quote: Quote) -> None:
                """Route a quote into any matching per-connection aggregator."""
                iid = str(quote.instrument.instrument_id)
                ts = quote.timestamp if quote.timestamp is not None else None
                if ts is None:
                    return
                from zoneinfo import ZoneInfo

                ist = ZoneInfo("Asia/Kolkata")
                ts_ist = (
                    ts.astimezone(ist).replace(tzinfo=None)
                    if ts.tzinfo is not None
                    else ts
                )
                for (bar_iid, _interval), agg in list(bar_aggregators.items()):
                    if bar_iid == iid:
                        price = quote.ltp.value
                        volume = quote.volume.value if quote.volume is not None else None
                        try:
                            agg.on_quote(ts_ist, price, volume)
                        except Exception:  # noqa: BLE001 — one bad tick never kills the socket
                            log.exception("bar aggregation failed for %s", iid)

            async def _subscribe_bars(msg: dict[str, Any]) -> None:
                raw = msg.get("bars") or msg.get("instruments")
                if not isinstance(raw, list) or not raw:
                    _ack({
                        "type": "error",
                        "message": "subscribe_bars needs 'bars': [{instrument, interval}]",
                    })
                    return
                added: list[tuple[str, str]] = []
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    iid = str(item.get("instrument", ""))
                    interval = str(item.get("interval", "1m"))
                    key = (iid, interval)
                    if key in bar_aggregators:
                        continue
                    try:
                        bar_aggregators[key] = _make_aggregator(iid, interval)
                    except (ValueError, KeyError) as exc:
                        _ack({"type": "error", "message": f"bad bar subscription {key}: {exc}"})
                        return
                    added.append(key)
                if not bar_disposables:
                    bar_disposables.append(bus.of_type(Quote).subscribe(_on_bar_quote))
                _ack({
                    "type": "subscribed_bars",
                    "bars": [f"{i}|{iv}" for i, iv in added],
                    "active": len(bar_aggregators),
                })

            def _unsubscribe_bars(msg: dict[str, Any]) -> None:
                raw = msg.get("bars")
                if not isinstance(raw, list):
                    return
                for item in raw:
                    if isinstance(item, dict):
                        key = (str(item.get("instrument", "")), str(item.get("interval", "")))
                    else:
                        iid, _, interval = str(item).partition("|")
                        key = (iid, interval)
                    bar_aggregators.pop(key, None)

            # --- Replay / simulation -----------------------------------------
            #
            # replay_start spawns a per-connection task that drives the SAME
            # aggregators with synthetic ticks over datalake history. Frames
            # are tagged source='sim' so the frontend can distinguish them.

            async def _replay_start(msg: dict[str, Any]) -> None:
                import asyncio as _asyncio

                instrument = str(msg.get("instrument", ""))
                interval = str(msg.get("interval", "1m"))
                method = str(msg.get("method", "anchored"))
                seed = msg.get("seed")
                speed = float(msg.get("speed", 1.0))
                if not instrument:
                    _ack({"type": "error", "message": "replay_start needs 'instrument'"})
                    return
                await _replay_stop(silent=True)

                async def _run() -> None:
                    from zoneinfo import ZoneInfo

                    from tradex_trading.reactive.bus import ReactiveBus
                    from tradex_trading.replay.synthetic_ticks import SyntheticTickGenerator

                    mini_bus = ReactiveBus()
                    ist = ZoneInfo("Asia/Kolkata")

                    gen = SyntheticTickGenerator(
                        mini_bus,
                        ticks_per_bar=int(msg.get("ticks_per_bar", 60)),
                        seed=seed if seed is None else int(seed),
                        method=method,
                    )

                    def _on_sim_quote(quote: Quote) -> None:
                        iid = str(quote.instrument.instrument_id)
                        ts = quote.timestamp
                        if ts is None:
                            return
                        ts_ist = (
                            ts.astimezone(ist).replace(tzinfo=None)
                            if ts.tzinfo is not None else ts
                        )
                        for (bar_iid, _iv), agg in list(bar_aggregators.items()):
                            if bar_iid == iid:
                                try:
                                    agg.on_quote(
                                        ts_ist,
                                        quote.ltp.value,
                                        quote.volume.value if quote.volume is not None else None,
                                    )
                                except Exception:  # noqa: BLE001
                                    log.exception("sim bar aggregation failed for %s", iid)

                    mini_bus.of_type(Quote).subscribe(_on_sim_quote)

                    # Load the requested window from the datalake and drive it.
                    from datetime import datetime as _dt
                    from datetime import timedelta as _td

                    from tradex_brokers.common.market_builders import candles_from_dataframe
                    from tradex_domain.enums import Timeframe as _TF

                    from tradex_trading.datalake.parquet_storage import ParquetStorage

                    store = ParquetStorage("data/")
                    symbol = instrument.split(":")[-1]
                    minutes = int(msg.get("minutes", 390))
                    # Anchor on the datalake's own last day when the trailing
                    # wall-clock window misses it (weekends, stale lake): a
                    # sim replays *recorded* history, so "latest available"
                    # beats "right now".
                    to_dt = _dt.now()
                    df = store.read(symbols=[symbol], start=to_dt - _td(minutes=minutes), end=to_dt)
                    if df.empty:
                        # Unknown symbol: date_range returns None (not a
                        # 2-tuple) — treat any non-tuple as "no coverage".
                        rng = store.date_range(symbol)
                        hi = rng[1] if isinstance(rng, tuple) else None
                        if hi is None:
                            _ack({"type": "error", "message": f"no datalake history for {symbol}"})
                            return
                        to_dt = hi
                        df = store.read(
                            symbols=[symbol], start=to_dt - _td(minutes=minutes), end=to_dt
                        )
                    if df.empty:
                        _ack({
                            "type": "error",
                            "message": f"no datalake history in last {minutes}m for {symbol}",
                        })
                        return
                    from tradex_domain.instruments import Equity

                    sim_instrument = Equity.of(instrument.split(":")[0], symbol)
                    candles = candles_from_dataframe(sim_instrument, df, timeframe=_TF.M1)

                    def _current_delay() -> float:
                        # Read live so a mid-run replay_speed takes effect on
                        # the next bar instead of being ignored.
                        return 1.0 / max(float(replay_state.get("speed", speed)), 0.01) / 60.0

                    for candle in candles:
                        if replay_state["task"] is None:
                            return  # stopped
                        while replay_state.get("paused"):
                            await _asyncio.sleep(0.05)
                        gen.feed_bar(candle)
                        await _asyncio.sleep(_current_delay())
                    # Flat-close whatever is open.
                    for agg in bar_aggregators.values():
                        try:
                            agg.flush()
                        except Exception:  # noqa: BLE001
                            pass
                    _ack({"type": "replay_done"})

                replay_state["task"] = _asyncio.create_task(_run())
                replay_state["paused"] = False
                replay_state["speed"] = speed
                _ack({"type": "replay_started", "instrument": instrument, "interval": interval})

            async def _replay_pause() -> None:
                replay_state["paused"] = True
                _ack({"type": "replay_paused"})

            async def _replay_resume() -> None:
                replay_state["paused"] = False
                _ack({"type": "replay_resumed"})

            async def _replay_speed(msg: dict[str, Any]) -> None:
                try:
                    replay_state["speed"] = float(msg.get("speed", 1.0))
                except (TypeError, ValueError):
                    _ack({"type": "error", "message": "speed must be a number"})
                    return
                _ack({"type": "replay_speed", "speed": replay_state["speed"]})

            async def _replay_stop(silent: bool = False) -> None:
                import asyncio as _asyncio

                task = replay_state.get("task")
                replay_state["task"] = None
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except (_asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                if not silent:
                    _ack({"type": "replay_stopped"})

            async def _snapshot(instruments: list[Any]) -> None:
                """Push one-shot REST quotes (and depth when requested).

                Opt-in (``"snapshot": true``) because a large set would hammer
                the provider's REST endpoint. Fetches run concurrently; a
                slow/failed fetch for one instrument never blocks the rest.
                """
                def _fetch(inst: Any) -> None:
                    iid = inst.instrument_id
                    try:
                        quote = s.broker.get_quote(inst)
                    except Exception:  # noqa: BLE001 — best-effort snapshot
                        quote = None
                    if quote is not None and iid in wanted:
                        loop.call_soon_threadsafe(_send_quote, quote)
                    if depth_mode.get(iid, "off") != "off":
                        try:
                            depth = s.broker.depth(inst)
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
            # Cancel any in-flight replay: cancel + await inside the live loop
            # (run_until_complete would raise "loop is running" here).
            replay_task = replay_state.get("task")
            replay_state["task"] = None
            if replay_task is not None:
                replay_task.cancel()
                try:
                    await replay_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 – teardown
                    pass
            bar_aggregators.clear()
            for d in bar_disposables:
                try:
                    d.dispose()
                except Exception:  # pragma: no cover – defensive
                    pass
            for d in disposables:
                try:
                    d.dispose()
                except Exception:  # pragma: no cover – defensive
                    pass

    # Built frontend, when present: single origin for API + UI (no CORS in
    # production). Absent dist = API-only app, byte-identical to before.
    if _UI_DIST_DIR.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/ui", StaticFiles(directory=_UI_DIST_DIR, html=True), name="ui")

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
        results = list(session.broker.search(stripped))
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
            quotes = session.broker.quote_batch(instruments)
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
                quote = session.broker.ltp(inst)
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


# The actual definitions live in :mod:`tradex_trading.interface._helpers`
# (so route modules can import them without a cycle). Re-exported here
# for back-compat with code that imports them off fastapi_app.
from tradex_trading.interface._helpers import (  # noqa: F401
    is_ready as _is_ready,
    readiness as _readiness,
    serialize_position as _serialize_position,
)


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
