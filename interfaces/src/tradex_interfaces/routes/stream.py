"""``/ws/stream`` WebSocket handler.

Top-level async function extracted from ``fastapi_app.create_app``. All
closure state is now passed as explicit parameters — no hidden coupling to
the app factory. Per-connection state (queues, writer task, wanted-set,
depth-mode, bar aggregators, replay task) lives inside the function as
local variables and nested closures, which is correct: it is the actual
state of one connection, not a hidden dependency.

Registered with the FastAPI app via a 5-line wrapper in ``create_app``
that binds the four state values (``session``, ``feed_registry``,
``outbound_max``, ``normalize_depth``) and delegates here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import secrets
from datetime import UTC
from typing import Any
from urllib.parse import parse_qs

from fastapi import WebSocket, WebSocketDisconnect
from tradex_domain.errors import CapabilityNotSupportedError
from tradex_domain.timezones import IST_ZONE

from tradex_interfaces._helpers import feed_status_payload
from tradex_interfaces.queueing import (
    CONTROL_QUEUE_MAX,
    WS_CLOSE_CONTROL_OVERFLOW,
    _enqueue_control,
    _enqueue_drop_oldest,
)
from tradex_interfaces.replay_guard import ReplayGuard
from tradex_interfaces.replay_run import ReplayRun
from tradex_interfaces.routes.stream_indicators import IndicatorStreamRegistry

log = logging.getLogger(__name__)

#: How often a connection re-checks feed health for an unsolicited
#: ``feed_status`` push. Cheap (a supervisor read) and only a *transition*
#: emits a frame.
FEED_STATUS_POLL_SECONDS = 2.0


async def ws_stream(
    app: Any,
    ws: WebSocket,
    session: Any,
    registry: Any,
    outbound_max: int,
    normalize_depth: Any,
    *,
    replay_guard: ReplayGuard | None = None,
) -> None:
    """Bridge the ReactiveBus onto a single /ws/stream client.

    Args:
        app: The FastAPI app (kept for symmetry / future state lookups).
        ws: The WebSocket connection.
        session: The bound ``TradingSession`` (``None`` for paper).
        registry: The per-app ``FeedRegistry`` (``None`` if no live feed).
        outbound_max: Per-connection outbound tick queue capacity.
        normalize_depth: Depth-mode normalizer shared with MarketFeed.
    """
    if session is None:
        await ws.close(code=1011, reason="no session bound")
        return

    # D: Metric counter for outbound queue drops; incremented alongside
    # dropped[0] so operators can alert on feed_queue_drops_total.
    _session_metrics = getattr(session, "metrics", None)
    _drops_counter = (
        _session_metrics.counter("feed_queue_drops_total")
        if _session_metrics is not None
        else None
    )

    def _track_drop(n: int) -> int:
        if n > 0 and _drops_counter is not None:
            _drops_counter.inc(n)
        return n

    def _control(payload: dict[str, Any]) -> None:
        """Enqueue a control frame, never evicting a queued one.

        Returns nothing; on overflow the connection is flagged for teardown
        so the client reconnects and re-syncs instead of drifting.
        """
        if not _enqueue_control(control, payload):
            if not control_overflow[0]:
                control_overflow[0] = True
                _track_drop(1)
                log.error(
                    "control queue overflow (cap=%d) for %s; closing client "
                    "to force resync rather than dropping order events",
                    CONTROL_QUEUE_MAX,
                    payload.get("type"),
                )

    # Auth gate — browsers cannot set headers on a WebSocket, so the key is
    # delivered as ?api_key=<value> in the connect URL and validated here,
    # BEFORE accept(), so an anonymous client never completes the handshake.
    # Constant-time compare. When no key is configured (paper/dev) the WS
    # stays open as before.
    _expected = getattr(app.state, "api_key", None)
    if _expected is not None:
        _provided = parse_qs(ws.url.query).get("api_key", [None])[0]
        if not secrets.compare_digest(str(_provided), str(_expected)):
            await ws.close(code=1008, reason="invalid api key")
            return

    await ws.accept()
    disposables: list[Any] = []
    loop = asyncio.get_running_loop()
    if registry is not None:
        registry.acquire()
    # Per-connection wanted set: instrument id -> Instrument (None if the
    # session has no live feed, e.g. paper mode — subscriptions still ack).
    wanted: dict[Any, Any] = {}
    depth_mode: dict[Any, str] = {}
    # Bounded outbound queues + single writer task: producers enqueue
    # (never await the socket) so a slow consumer cannot buffer the
    # event loop's memory without bound. Control messages (acks/fills)
    # get strict priority over ticks. Overflow policy differs by class:
    # ticks drop the oldest (freshness-bound market data), while control
    # frames are NEVER evicted — a lost fill is a false view of real
    # money, so an overflowing control queue marks the connection for
    # termination and the client reconnects and re-syncs a snapshot.
    ticks: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=outbound_max)
    control: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=CONTROL_QUEUE_MAX)
    dropped: list[int] = [0]  # closure cell — incremented by producers
    # Set when a control frame could not be enqueued. The writer drains
    # what it can, then closes the socket rather than leaving the client
    # silently desynchronized from OMS state.
    control_overflow: list[bool] = [False]
    if replay_guard is None:
        replay_guard = getattr(app.state, "replay_guard", None)
    if replay_guard is None:
        replay_guard = ReplayGuard()
        app.state.replay_guard = replay_guard
    replay_run: ReplayRun | None = None

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

        async def _close_on_control_overflow() -> None:
            """Close the socket after an unreportable control-queue overflow.

            A control frame was rejected, so this client is now missing OMS
            truth (a fill, a cancel, a rejection). Serving it further would
            leave a trader looking at a working order the venue already
            filled, so the connection is closed with a non-lossy code and the
            client reconnects and re-syncs a fresh snapshot.
            """
            try:
                await ws.close(
                    code=WS_CLOSE_CONTROL_OVERFLOW,
                    reason="control queue overflow; reconnect to resync",
                )
            except Exception:  # socket already gone; receive loop cleans up
                pass

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
                if control_overflow[0]:
                    # Nothing left to drain, but a control frame was already
                    # lost. Do not block on an empty queue waiting for traffic
                    # that can no longer restore this client's view.
                    await _close_on_control_overflow()
                    return
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
            if control_overflow[0]:
                # Flagged while draining. Everything still queued has now been
                # sent, so close and force the client to re-sync rather than
                # leaving a permanent hole in its order history.
                await _close_on_control_overflow()
                return

    writer_task = asyncio.create_task(_writer())
    status_tasks: list[asyncio.Task[Any]] = []

    try:
        from tradex_brokers.common.provider_common import instrument_from_id
        from tradex_domain.events import OrderFilled, PositionUpdated
        from tradex_domain.market import Depth, Quote
        from tradex_domain.value_objects import InstrumentId

        bus = session.bus

        last_ltp: dict[Any, float] = {}

        def _send_quote(quote: Quote) -> None:
            if quote.instrument.instrument_id not in wanted:
                return
            last_ltp[quote.instrument.instrument_id] = float(quote.ltp.value)
            dropped[0] += _track_drop(_enqueue_drop_oldest(ticks, {
                "type": "quote",
                "instrument": str(quote.instrument.instrument_id),
                "ltp": str(quote.ltp.value),
                "bid": str(quote.bid.value) if quote.bid is not None else None,
                "ask": str(quote.ask.value) if quote.ask is not None else None,
            }))

        def _send_depth(depth: Depth) -> None:
            # Engine MarketDepth contract (openalgo-charts src/feed/types.ts:28):
            # object levels, numeric values, ltp required. LTP falls back to the
            # best bid (openalgo-ws.ts:363 does the same for its own adapter).
            iid = depth.instrument.instrument_id
            if iid not in wanted or depth_mode.get(iid) == "off":
                return
            bids = [
                {"price": float(price.value), "qty": float(qty.value)}
                for price, qty in depth.bids
            ]
            asks = [
                {"price": float(price.value), "qty": float(qty.value)}
                for price, qty in depth.asks
            ]
            ltp = last_ltp.get(iid)
            if ltp is None:
                ltp = bids[0]["price"] if bids else (asks[0]["price"] if asks else 0.0)
            dropped[0] += _track_drop(_enqueue_drop_oldest(ticks, {
                "type": "depth",
                "instrument": str(iid),
                "bids": bids,
                "asks": asks,
                "ltp": ltp,
            }))

        def _send_fill(event: OrderFilled) -> None:
            fill = event.fill
            _control({
                "type": "fill",
                "order_id": str(fill.order_id),
                "instrument": str(fill.instrument.instrument_id),
                "price": str(fill.price.value),
                "quantity": str(fill.quantity.value),
            })

        def _send_position(event: PositionUpdated) -> None:
            position = event.position
            if position.instrument.instrument_id not in wanted:
                return
            _control({
                "type": "position",
                "instrument": str(position.instrument.instrument_id),
                "quantity": str(position.quantity.value),
                "avg_price": str(position.avg_price.value),
                "realized_pnl": str(position.realized_pnl.amount),
                "unrealized_pnl": str(position.unrealized_pnl.amount),
                "mark_price": str(position.mark_price.value) if position.mark_price else None,
                "marked_at": position.marked_at.isoformat() if position.marked_at else None,
                "mark_source": position.mark_source,
            })

        def _send_order(payload: Any) -> None:
            """Forward a broker order update onto the priority control queue."""
            order = getattr(payload, "order", payload)
            _control({
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
            _control(message)

        def _drop_replay_frames(run_id: str) -> None:
            retained: list[dict[str, Any]] = []
            while True:
                try:
                    frame = ticks.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if frame.get("type") != "bar" or frame.get("source") != "sim" or frame.get("run_id") != run_id:
                    retained.append(frame)
            for frame in retained:
                ticks.put_nowait(frame)

        status_baseline: list[Any] = [None]

        async def _feed_status_watch() -> None:
            """Push ``feed_status`` when the live-order gate state changes.

            The first observation is recorded silently, so a client connecting
            to an already-healthy feed gets no unsolicited frame; only a real
            transition (READY -> DEGRADED/HALTED, generation bump) is pushed.
            ``/health/ready`` and the explicit ``feed_status`` request stay the
            pull paths.
            """
            while True:
                await asyncio.sleep(FEED_STATUS_POLL_SECONDS)
                payload = feed_status_payload(session)
                key = (payload["state"], payload["ready"], payload["generation"])
                if status_baseline[0] is None:
                    status_baseline[0] = key
                    continue
                if key != status_baseline[0]:
                    status_baseline[0] = key
                    _ack(payload)

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
            # Feed gate for live order controls: the UI must not enable a
            # live order button while the feed is not READY.
            status = feed_status_payload(session)
            feed_info["feed_state"] = status["state"]
            feed_info["feed_ready"] = status["ready"]
            feed_info["live_orders_enabled"] = status["live_orders_enabled"]
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
            elif msg_type == "indicator-sub":
                _indicator_sub(msg)
            elif msg_type == "indicator-unsub":
                _indicator_unsub(msg)
            elif msg_type == "replay_start":
                await _replay_start(msg)
            elif msg_type == "replay_pause":
                await _replay_pause()
            elif msg_type == "replay_resume":
                await _replay_resume()
            elif msg_type == "replay_speed":
                await _replay_speed(msg)
            elif msg_type == "replay_step":
                await _replay_step()
            elif msg_type == "replay_seek":
                await _replay_seek(msg)
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
            elif msg_type == "feed_status":
                # Pull path for the live-order gate (state/ready/generation/
                # last_event_at/age). Pushed automatically on transition.
                _ack(feed_status_payload(session))
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
                # Live broker order updates — backend if bound, else fall
                # back to the reactive bus's OrderPlaced stream (the same
                # path StreamService used; inlined here per G8 since the
                # service layer is gone).
                from tradex_domain.events import OrderPlaced

                from tradex_runtime.streaming import (
                    BackendStreamSubscription,
                    StreamSubscription,
                )

                backend = getattr(session, "_stream_backend", None)
                if backend is not None and hasattr(backend, "subscribe_orders"):
                    sub = BackendStreamSubscription(
                        backend, backend.subscribe_orders(_on_order), "orders",
                    )
                else:
                    disposable = session.bus.of_type(OrderPlaced).subscribe(_on_order)
                    sub = StreamSubscription(disposable, "orders")  # type: ignore[assignment]
                handle = sub
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
        step_closed = False
        last_closed_bar_index = 0

        def _normalize_ws_interval(interval: str) -> str:
            """REST daily is ``D``; domain Timeframe / WS is ``1d``."""
            return "1d" if interval in ("D", "1D", "d") else interval

        def _send_bar_frame(frame: Any) -> None:
            nonlocal step_closed, last_closed_bar_index
            if (
                getattr(frame, "closed", False) is True
                and getattr(frame, "source", None) == "sim"
                and replay_run is not None
                and getattr(frame, "run_id", None) == replay_run.run_id
                and frame.instrument == replay_run.instrument
                and frame.timeframe == replay_run.timeframe.value
            ):
                step_closed = True
                try:
                    last_closed_bar_index = replay_run.target_keys.index(int(frame.time))
                except ValueError:
                    pass
            dropped[0] += _track_drop(_enqueue_drop_oldest(ticks, {
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
                "run_id": getattr(frame, "run_id", None),
                "provisional": bool(getattr(frame, "provisional", False)),
            }))
            indicator_registry.handle_bar_frame(frame)
            # A1 leftover: wire live closed bars into MISSING_BAR integrity tracker.
            # Sim bars (source="sim") must never reach the integrity tracker — replay
            # quotes are synthetic and gap detection against them is meaningless.
            if getattr(frame, "closed", False) is True and getattr(frame, "source", "live") == "live":
                _mf = getattr(session, "market_feed", None)
                if _mf is not None:
                    try:
                        _mf.on_bar_frame(frame)
                    except Exception:
                        log.exception("on_bar_frame integrity hook failed for %s", frame.instrument)

        # --- Indicator push (Tier-2 subscribe seam) ----------------------
        indicator_registry = IndicatorStreamRegistry(
            lambda frame_dict: _track_drop(_enqueue_drop_oldest(ticks, frame_dict))
        )

        def _indicator_sub(msg: dict[str, Any]) -> None:
            items = msg.get("indicators")
            if not isinstance(items, list) or not items:
                _ack({
                    "type": "error",
                    "message": (
                        "indicator-sub needs 'indicators': "
                        "[{instrument, interval, ids, mode?}]"
                    ),
                })
                return
            accepted = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    ids = indicator_registry.add(
                        str(item.get("instrument", "")),
                        str(item.get("interval", "1m")),
                        [str(i) for i in (item.get("ids") or [])],
                        str(item.get("mode", "bar-close")),
                    )
                except ValueError as exc:
                    _ack({"type": "error", "message": str(exc)})
                    return
                accepted.append({
                    "instrument": str(item.get("instrument", "")),
                    "interval": str(item.get("interval", "1m")),
                    "ids": ids,
                    "mode": str(item.get("mode", "bar-close")),
                })
            _ack({"type": "indicator-subscribed", "indicators": accepted})

        def _indicator_unsub(msg: dict[str, Any]) -> None:
            items = msg.get("indicators")
            if not isinstance(items, list) or not items:
                _ack({
                    "type": "error",
                    "message": (
                        "indicator-unsub needs 'indicators': "
                        "[{instrument, interval, ids?}]"
                    ),
                })
                return
            removed = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                ids = item.get("ids")
                dropped_ids = indicator_registry.remove(
                    str(item.get("instrument", "")),
                    str(item.get("interval", "1m")),
                    [str(i) for i in ids] if ids else None,
                )
                removed.append({
                    "instrument": str(item.get("instrument", "")),
                    "interval": str(item.get("interval", "1m")),
                    "ids": dropped_ids,
                })
            _ack({"type": "indicator-unsubscribed", "indicators": removed})

        def _make_aggregator(
            inst_id: str,
            interval: str,
            *,
            run_id: str | None = None,
            provisional: bool = False,
        ) -> Any:
            from tradex_domain.enums import Timeframe

            from tradex_runtime.bar_aggregator import BarAggregator

            return BarAggregator(
                inst_id,
                Timeframe(_normalize_ws_interval(interval)),
                on_frame=_send_bar_frame,
                run_id=run_id,
                provisional=provisional,
            )

        def _seed_number(raw: Any, name: str, *, default: float | None = None) -> float:
            if raw is None and default is not None:
                raw = default
            if isinstance(raw, bool):
                raise ValueError(f"{name} must be a number")
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be a number") from exc
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            return value

        def _seed_frame(
            raw: Any,
            *,
            iid: str,
            interval: str,
            provisional: bool,
            closed: bool,
        ) -> Any:
            from tradex_runtime.bar_aggregator import BarFrame

            if not isinstance(raw, dict):
                raise ValueError("bar seed must be an object")
            time_value = _seed_number(raw.get("time"), "time")
            if time_value < 0 or not time_value.is_integer():
                raise ValueError("time must be a non-negative integer")
            return BarFrame(
                instrument=iid,
                timeframe=interval,
                time=int(time_value),
                open=_seed_number(raw.get("open"), "open"),
                high=_seed_number(raw.get("high"), "high"),
                low=_seed_number(raw.get("low"), "low"),
                close=_seed_number(raw.get("close"), "close"),
                volume=_seed_number(raw.get("volume"), "volume", default=0.0),
                closed=closed,
                source="live",
                provisional=provisional,
            )

        def _parse_bar_seed(
            item: dict[str, Any],
            *,
            iid: str,
            interval: str,
        ) -> tuple[Any | None, Any | None, bool]:
            has_provisional = "provisional" in item
            provisional = item.get("provisional", True)
            if not isinstance(provisional, bool):
                raise ValueError("provisional must be a boolean")
            raw_seed = item.get("seed")
            if raw_seed is None:
                raw_seed = {}
            if not isinstance(raw_seed, dict):
                raise ValueError("seed must be an object")
            last_closed = raw_seed.get("lastClosed")
            current_bucket = raw_seed.get("currentBucket")
            if not has_provisional:
                provisional = current_bucket is None
            last_frame = (
                _seed_frame(
                    last_closed,
                    iid=iid,
                    interval=interval,
                    provisional=False,
                    closed=True,
                )
                if last_closed is not None
                else None
            )
            current_frame = (
                _seed_frame(
                    current_bucket,
                    iid=iid,
                    interval=interval,
                    provisional=provisional,
                    closed=False,
                )
                if current_bucket is not None
                else None
            )
            return last_frame, current_frame, provisional

        def _on_bar_quote(quote: Quote) -> None:
            """Route a quote into any matching per-connection aggregator."""
            iid = str(quote.instrument.instrument_id)
            ts = quote.timestamp if quote.timestamp is not None else None
            if ts is None:
                return
            ist = IST_ZONE
            ts_ist = (
                ts.astimezone(ist).replace(tzinfo=None)
                if ts.tzinfo is not None
                else ts
            )
            for (bar_iid, _bar_iv), agg in list(bar_aggregators.items()):
                if bar_iid != iid:
                    continue
                price = quote.ltp.value
                volume = quote.volume.value if quote.volume is not None else None
                try:
                    agg.on_quote(ts_ist, price, volume)
                except Exception:
                    log.exception("bar aggregation failed for %s", iid)

        def _on_bar_quote_threadsafe(quote: Quote) -> None:
            try:
                loop.call_soon_threadsafe(_on_bar_quote, quote)
            except RuntimeError:
                return

        async def _subscribe_bars(msg: dict[str, Any]) -> None:
            raw = msg.get("bars") or msg.get("instruments")
            if not isinstance(raw, list) or not raw:
                _ack({
                    "type": "error",
                    "message": "subscribe_bars needs 'bars': [{instrument, interval}]",
                })
                return
            added: list[tuple[str, str]] = []
            pending: list[tuple[tuple[str, str], Any]] = []
            pending_keys: set[tuple[str, str]] = set()
            for item in raw:
                if not isinstance(item, dict):
                    continue
                iid = str(item.get("instrument", ""))
                interval = _normalize_ws_interval(str(item.get("interval", "1m")))
                key = (iid, interval)
                if key in pending_keys:
                    continue
                try:
                    last_closed, current_bucket, provisional = _parse_bar_seed(
                        item,
                        iid=iid,
                        interval=interval,
                    )
                except (ValueError, KeyError, TypeError, OverflowError, OSError) as exc:
                    for _, candidate in pending:
                        candidate.dispose(flush=False)
                    _ack({"type": "error", "message": f"bad bar subscription {key}: {exc}"})
                    return
                existing = bar_aggregators.get(key)
                if existing is not None:
                    if last_closed is not None:
                        existing.seed_last_closed(last_closed)
                    if current_bucket is not None:
                        existing.seed_current_bucket(current_bucket, provisional=provisional)
                    continue
                try:
                    aggregator = _make_aggregator(
                        iid,
                        interval,
                        provisional=provisional,
                    )
                    if last_closed is not None:
                        aggregator.seed_last_closed(last_closed)
                    if current_bucket is not None:
                        aggregator.seed_current_bucket(
                            current_bucket,
                            provisional=provisional,
                        )
                except (ValueError, KeyError, TypeError, OverflowError, OSError) as exc:
                    _ack({"type": "error", "message": f"bad bar subscription {key}: {exc}"})
                    return
                pending.append((key, aggregator))
                pending_keys.add(key)
                added.append(key)
            for key, aggregator in pending:
                bar_aggregators[key] = aggregator
            if not bar_disposables:
                bar_disposables.append(bus.of_type(Quote).subscribe(_on_bar_quote_threadsafe))
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
                    key = (
                        str(item.get("instrument", "")),
                        _normalize_ws_interval(str(item.get("interval", ""))),
                    )
                else:
                    iid, _, interval = str(item).partition("|")
                    key = (iid, _normalize_ws_interval(interval))
                bar_aggregators.pop(key, None)

        replay_config: dict[str, Any] = {}
        replay_cleaning: set[str] = set()
        replay_finished: set[str] = set()
        replay_cleanup_tasks: set[asyncio.Task[Any]] = set()
        replay_stop_ack_sent = False

        def _replay_error(message: str, run_id: str | None = None) -> None:
            frame: dict[str, Any] = {
                "type": "replay_error",
                "message": str(message),
            }
            if run_id is not None:
                frame["run_id"] = run_id
            dropped[0] += _track_drop(_enqueue_drop_oldest(ticks, frame))

        def _bounded_float(raw: Any, name: str, default: float, low: float, high: float) -> float:
            if raw is None:
                raw = default
            if isinstance(raw, bool):
                raise ValueError(f"{name} must be a number")
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be a number") from exc
            if not math.isfinite(value) or value < low or value > high:
                raise ValueError(f"{name} must be between {low:g} and {high:g}")
            return value

        def _bounded_int(raw: Any, name: str, default: int, low: int, high: int) -> int:
            if raw is None:
                raw = default
            if isinstance(raw, bool):
                raise ValueError(f"{name} must be an integer")
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be an integer") from exc
            if not math.isfinite(value) or value != int(value) or not low <= int(value) <= high:
                raise ValueError(f"{name} must be between {low} and {high}")
            return int(value)

        def _replay_start_params(msg: dict[str, Any]) -> dict[str, Any]:
            from tradex_domain.enums import Timeframe

            raw_instrument = msg.get("instrument")
            if not isinstance(raw_instrument, str) or not raw_instrument.strip():
                raise ValueError("replay_start needs 'instrument'")
            instrument = raw_instrument.strip()
            if ":" not in instrument or any(not part for part in instrument.split(":", 1)):
                raise ValueError("replay instrument must be EXCHANGE:SYMBOL")
            interval_value = _normalize_ws_interval(str(msg.get("interval", "1m")))
            try:
                timeframe = Timeframe(interval_value)
            except ValueError as exc:
                raise ValueError(f"unsupported replay interval {interval_value!r}") from exc
            if timeframe.value not in {"1m", "5m", "15m", "30m", "1h", "1d"}:
                raise ValueError(f"unsupported replay interval {interval_value!r}")
            method = msg.get("method", "ohlc")
            if not isinstance(method, str) or method not in {"ohlc", "anchored", "bridge"}:
                raise ValueError("method must be ohlc, anchored, or bridge")
            seed = None
            if msg.get("seed") is not None:
                seed = _bounded_int(msg.get("seed"), "seed", 0, -(2**63), 2**63)
            speed = _bounded_float(msg.get("speed"), "speed", 1.0, 0.01, 1000.0)
            ticks_per_bar = _bounded_int(msg.get("ticks_per_bar"), "ticks_per_bar", 10, 2, 1000)
            minutes = _bounded_int(msg.get("minutes"), "minutes", 390, 1, 10080)
            start_time = None
            if msg.get("start_time") is not None:
                start_time = _bounded_int(msg.get("start_time"), "start_time", 0, 0, 10**12)
            return {
                "instrument": instrument,
                "timeframe": timeframe,
                "interval": timeframe.value,
                "method": method,
                "seed": seed,
                "speed": speed,
                "ticks_per_bar": ticks_per_bar,
                "minutes": minutes,
                "start_time": start_time,
            }

        def _candle_seconds(candle: Any) -> int:
            timestamp = candle.timestamp
            if timestamp is None:
                raise ValueError("replay candle has no timestamp")
            ist = IST_ZONE
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=ist)
            else:
                timestamp = timestamp.astimezone(ist)
            return int(timestamp.timestamp())

        def _load_replay_candles(params: dict[str, Any]) -> tuple[Any, ...]:
            from datetime import datetime, timedelta

            from tradex_domain.enums import Timeframe
            from tradex_domain.instruments import Equity

            from tradex_market_data.market_provider import ParquetMarketProvider
            from tradex_market_data.parquet_storage import ParquetStorage
            from tradex_market_data.paths import DATALAKE_ROOT

            instrument = params["instrument"]
            exchange, symbol = instrument.split(":", 1)
            equity = Equity.of(exchange, symbol)
            store = ParquetStorage(DATALAKE_ROOT)
            provider = ParquetMarketProvider(store=store)
            now = datetime.now(IST_ZONE).replace(tzinfo=None)
            start_time = params["start_time"]
            ist = IST_ZONE
            if start_time is None:
                from_dt = now - timedelta(minutes=params["minutes"])
            else:
                from_dt = (
                    datetime.fromtimestamp(start_time, tz=ist).replace(tzinfo=None)
                    - timedelta(days=3)
                )
            series = provider.history(equity, Timeframe.M1, from_dt, now)
            if not series.candles:
                date_range = store.date_range(symbol)
                high = (
                    date_range[1]
                    if isinstance(date_range, (tuple, list)) and len(date_range) > 1
                    else None
                )
                if high is None:
                    raise ValueError(f"no datalake history for {symbol}")
                now = high if getattr(high, "tzinfo", None) is None else high.replace(tzinfo=None)
                if start_time is None:
                    from_dt = now - timedelta(minutes=params["minutes"])
                else:
                    from_dt = (
                        datetime.fromtimestamp(start_time, tz=ist).replace(tzinfo=None)
                        - timedelta(days=3)
                    )
                series = provider.history(equity, Timeframe.M1, from_dt, now)
            candles = tuple(series.candles)
            if start_time is not None:
                candles = tuple(c for c in candles if _candle_seconds(c) >= start_time)
            if not candles:
                raise ValueError(f"no datalake history in last {params['minutes']}m for {symbol}")
            if any(candle.timeframe != Timeframe.M1 for candle in candles):
                raise ValueError("replay dataset must contain M1 candles")
            return tuple(sorted(candles, key=_candle_seconds))

        def _target_keys(candles: tuple[Any, ...], timeframe: Any) -> tuple[int, ...]:
            from tradex_runtime.bar_aggregator import _tf_seconds, bucket_start

            seconds = _tf_seconds(timeframe)
            keys: list[int] = []
            ist = IST_ZONE
            for candle in candles:
                timestamp = candle.timestamp
                if timestamp.tzinfo is not None:
                    timestamp = timestamp.astimezone(ist).replace(tzinfo=None)
                start = bucket_start(timestamp, seconds)
                key = int(start.replace(tzinfo=ist).timestamp())
                if not keys or key != keys[-1]:
                    keys.append(key)
            return tuple(keys)

        def _socket_writable() -> bool:
            state = getattr(ws, "client_state", None)
            return state is None or "DISCONNECTED" not in str(state).upper()

        async def _finish_run(
            run: ReplayRun,
            terminal_type: str,
            message: str | None = None,
            *,
            emit: bool = True,
        ) -> None:
            nonlocal replay_run
            if run.run_id in replay_cleaning or run.run_id in replay_finished:
                return
            replay_cleaning.add(run.run_id)
            try:
                try:
                    await run.cancel_and_drain()
                except Exception as exc:
                    log.exception("replay cleanup failed for %s", run.run_id)
                    if message is None:
                        message = str(exc)
                current_run = replay_run is run
                if current_run:
                    replay_run = None
                try:
                    replay_guard.release(run.run_id)
                except Exception:
                    log.exception("replay guard release failed for %s", run.run_id)
                if not current_run:
                    return
                replay_config.clear()
                if emit and _socket_writable():
                    _drop_replay_frames(run.run_id)
                    frame: dict[str, Any] = {"type": terminal_type, "run_id": run.run_id}
                    if message is not None:
                        frame["message"] = message
                    _ack(frame)
            finally:
                replay_cleaning.discard(run.run_id)
                replay_finished.add(run.run_id)

        async def _replay_start(
            msg: dict[str, Any],
            dataset: tuple[Any, ...] | None = None,
        ) -> None:
            nonlocal replay_run, replay_stop_ack_sent, step_closed, last_closed_bar_index
            try:
                params = _replay_start_params(msg)
            except ValueError as exc:
                _replay_error(str(exc))
                return
            try:
                candles = tuple(dataset) if dataset is not None else _load_replay_candles(params)
                if not candles:
                    raise ValueError("replay dataset is empty")
            except Exception as exc:
                _replay_error(str(exc))
                return
            try:
                target_keys = _target_keys(candles, params["timeframe"])
                run = ReplayRun(
                    run_id=secrets.token_hex(12),
                    instrument=params["instrument"],
                    timeframe=params["timeframe"],
                    candles=candles,
                    target_keys=target_keys,
                    speed=params["speed"],
                )
            except Exception as exc:
                _replay_error(str(exc))
                return
            try:
                run.aggregator = _make_aggregator(
                    run.instrument,
                    run.timeframe.value,
                    run_id=run.run_id,
                )
            except Exception as exc:
                run.close()
                _replay_error(f"cannot start replay bars: {exc}")
                return
            # Release the previous run (and its guard slot) before acquiring
            # the new one — one active replay per process.
            if replay_run is not None:
                await _replay_stop(silent=True)
            acquired = False
            try:
                replay_guard.acquire(run.run_id)
                acquired = True
            except Exception as exc:
                if acquired:
                    replay_guard.release(run.run_id)
                run.close()
                _replay_error(f"cannot start replay: {exc}")
                return
            replay_run = run
            replay_finished.clear()
            replay_stop_ack_sent = False
            run.paused = bool(msg.get("paused", False))
            replay_config.update(params)
            step_closed = False
            last_closed_bar_index = 0
            wall_seconds_per_bar = 1.0 / max(run.speed, 0.01)
            try:
                _ack({
                    "type": "replay_started",
                    "run_id": run.run_id,
                    "instrument": run.instrument,
                    "interval": run.timeframe.value,
                    "start_time": target_keys[0],
                    "total_bars": len(target_keys),
                    "simulated": True,
                    "wall_seconds_per_bar": wall_seconds_per_bar,
                })
            except Exception as exc:
                await _finish_run(run, "replay_error", str(exc))
                return

            async def _run() -> None:
                nonlocal step_closed, last_closed_bar_index
                from tradex_reactive.bus import ReactiveBus
                from tradex_replay.synthetic_ticks import SyntheticTickGenerator

                try:
                    generator = SyntheticTickGenerator(
                        ReactiveBus(),
                        ticks_per_bar=params["ticks_per_bar"],
                        seed=params["seed"],
                        method=params["method"],
                    )
                except Exception as exc:
                    await _finish_run(run, "replay_error", str(exc))
                    return

                async def _feed(candle: Any, *, wait: bool) -> None:
                    quotes = generator.iter_ticks(candle)
                    n_prints = max(len(quotes), 1)
                    delay = (1.0 / max(run.speed, 0.01)) / n_prints if wait else 0.0
                    for quote in quotes:
                        # Pause only while playing. Step clears step_requested
                        # before feeding and must not block on paused=True.
                        if wait:
                            while (
                                run.paused
                                and not run.step_requested
                                and not run.terminal
                            ):
                                await asyncio.sleep(0.01)
                        if run.terminal:
                            return
                        timestamp = quote.timestamp
                        if timestamp is None:
                            await _finish_run(
                                run,
                                "replay_error",
                                "replay quote has no timestamp",
                            )
                            return
                        if timestamp.tzinfo is not None:
                            timestamp = (
                                timestamp.astimezone(IST_ZONE)
                                .replace(tzinfo=None)
                            )
                        # Aggregator only — never session bus, quote frames, or last_ltp.
                        if run.aggregator is not None:
                            run.aggregator.on_quote(
                                timestamp,
                                quote.ltp.value,
                                quote.volume.value if quote.volume is not None else None,
                                source="sim",
                            )
                        if wait and delay > 0.001:
                            await asyncio.sleep(delay)

                try:
                    while run.cursor < len(run.candles) and not run.terminal:
                        while run.paused and not run.step_requested and not run.terminal:
                            await asyncio.sleep(0.01)
                        if run.terminal:
                            return
                        stepping = run.step_requested
                        if stepping:
                            run.step_requested = False
                            step_closed = False
                        candle = run.candles[run.cursor]
                        run.cursor += 1
                        await _feed(candle, wait=not stepping)
                        if stepping:
                            if run.timeframe.value == "1m":
                                # One M1 candle is one target bar — flush to
                                # close without burning the next minute.
                                if not step_closed and run.aggregator is not None:
                                    run.aggregator.flush()
                            else:
                                # Higher TF: keep feeding M1s until the
                                # target bucket closes, then flush if needed.
                                while (
                                    not step_closed
                                    and run.cursor < len(run.candles)
                                    and not run.terminal
                                ):
                                    await _feed(run.candles[run.cursor], wait=False)
                                    run.cursor += 1
                                if not step_closed and run.aggregator is not None:
                                    run.aggregator.flush()
                            if not run.terminal:
                                run.paused = True
                                # Let the single writer drain the closed bar
                                # before publishing the step acknowledgement.
                                # Both messages share the tick queue, but an
                                # explicit yield makes the ordering contract
                                # observable to the WebSocket writer.
                                await asyncio.sleep(0)
                                dropped[0] += _track_drop(_enqueue_drop_oldest(
                                    ticks,
                                    {
                                        "type": "replay_stepped",
                                        "run_id": run.run_id,
                                        "bar_index": last_closed_bar_index,
                                    },
                                ))
                                dropped[0] += _track_drop(_enqueue_drop_oldest(
                                    ticks,
                                    {"type": "replay_paused", "run_id": run.run_id},
                                ))
                    if not run.terminal:
                        if run.aggregator is not None:
                            run.aggregator.flush()
                        await _finish_run(run, "replay_done")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("replay task failed for %s", run.run_id)
                    await _finish_run(run, "replay_error", str(exc))

            run.task = asyncio.create_task(_run())

            def _task_done(task: asyncio.Task[None]) -> None:
                if task.cancelled():
                    return
                try:
                    error = task.exception()
                except asyncio.CancelledError:
                    return
                if error is not None and replay_run is run and not run.terminal:
                    cleanup_task = asyncio.create_task(
                        _finish_run(run, "replay_error", str(error))
                    )
                    replay_cleanup_tasks.add(cleanup_task)
                    cleanup_task.add_done_callback(replay_cleanup_tasks.discard)

            run.task.add_done_callback(_task_done)

        async def _replay_pause() -> None:
            payload: dict[str, Any] = {"type": "replay_paused"}
            if replay_run is not None:
                replay_run.paused = True
                payload["run_id"] = replay_run.run_id
            _ack(payload)

        async def _replay_resume() -> None:
            payload: dict[str, Any] = {"type": "replay_resumed"}
            if replay_run is not None:
                replay_run.paused = False
                payload["run_id"] = replay_run.run_id
            _ack(payload)

        async def _replay_step() -> None:
            if replay_run is not None:
                replay_run.step_requested = True

        async def _replay_seek(msg: dict[str, Any]) -> None:
            run = replay_run
            if run is None:
                _replay_error("replay_seek needs an active replay")
                return
            try:
                if msg.get("index") is not None or msg.get("target_index") is not None:
                    raw_index = msg.get("index", msg.get("target_index"))
                    index = _bounded_int(
                        raw_index,
                        "replay_seek index",
                        0,
                        0,
                        len(run.target_keys) - 1,
                    )
                    start_time = run.target_keys[index]
                else:
                    start_time = _bounded_int(
                        msg.get("start_time"),
                        "replay_seek start_time",
                        0,
                        0,
                        10**12,
                    )
                candle_index = run.target_index_for_key(start_time)
                if candle_index >= len(run.candles):
                    raise KeyError(start_time)
            except (KeyError, ValueError) as exc:
                _replay_error(str(exc), run.run_id)
                await _replay_stop()
                return
            params = dict(replay_config)
            params["start_time"] = start_time
            params["paused"] = False
            dataset = run.candles[candle_index:]
            previous_run_id = run.run_id
            await _replay_stop(silent=True)
            await _replay_start(params, dataset=dataset)
            if replay_run is None:
                _replay_error("replay seek restart failed", previous_run_id)

        async def _replay_speed(msg: dict[str, Any]) -> None:
            try:
                speed = _bounded_float(msg.get("speed"), "speed", 1.0, 0.01, 1000.0)
            except ValueError as exc:
                _replay_error(str(exc), replay_run.run_id if replay_run is not None else None)
                if replay_run is not None:
                    await _replay_stop()
                return
            payload: dict[str, Any] = {"type": "replay_speed", "speed": speed}
            if replay_run is not None:
                replay_run.speed = speed
                payload["run_id"] = replay_run.run_id
            _ack(payload)

        async def _replay_stop(silent: bool = False) -> None:
            nonlocal replay_run, replay_stop_ack_sent
            run = replay_run
            if run is None:
                if not silent and not replay_stop_ack_sent:
                    _ack({"type": "replay_stopped"})
                    replay_stop_ack_sent = True
                return
            await _finish_run(run, "replay_stopped", emit=not silent)
            if not silent:
                replay_stop_ack_sent = True

        async def _snapshot(instruments: list[Any]) -> None:
            """Push one-shot REST quotes (and depth when requested).

            Opt-in (``"snapshot": true``) because a large set would hammer
            the provider's REST endpoint. Fetches run concurrently; a
            slow/failed fetch for one instrument never blocks the rest.
            """
            def _fetch(inst: Any) -> None:
                iid = inst.instrument_id
                try:
                    quote = session.broker.get_quote(inst)
                except Exception:  # noqa: BLE001 — best-effort snapshot
                    quote = None
                if quote is None or (
                    getattr(session, "mode", None) == "paper" and float(quote.ltp.value) == 100.0
                ):
                    try:
                        from datetime import datetime, timedelta
                        from decimal import Decimal

                        from tradex_domain.enums import Timeframe
                        from tradex_domain.market import Quote
                        from tradex_domain.value_objects import Price

                        from tradex_market_data.market_provider import ParquetMarketProvider
                        from tradex_market_data.parquet_storage import ParquetStorage
                        from tradex_market_data.paths import DATALAKE_ROOT

                        store = ParquetStorage(DATALAKE_ROOT)
                        provider = ParquetMarketProvider(store=store)
                        sym = getattr(inst, "symbol", None) or str(iid).split(":")[-1]
                        rng = store.date_range(sym)
                        if rng and rng[1]:
                            series = provider.history(
                                inst,
                                Timeframe.M1,
                                rng[1] - timedelta(days=2),
                                rng[1],
                            )
                            if series.candles:
                                last_c = Decimal(
                                    str(round(float(series.candles[-1].ohlc.close.value), 2))
                                )
                                p_ltp = Price(last_c)
                                p_bid = Price(last_c - Decimal("0.05"))
                                p_ask = Price(last_c + Decimal("0.05"))
                                quote = Quote(
                                    instrument=inst,
                                    ltp=p_ltp,
                                    bid=p_bid,
                                    ask=p_ask,
                                    timestamp=datetime.now(UTC),
                                    exchange=getattr(
                                        getattr(inst, "exchange", None), "value", "NSE"
                                    ),
                                    provider="paper",
                                )
                                if hasattr(session.broker, "set_quote"):
                                    session.broker.set_quote(inst, ltp=p_ltp, bid=p_bid, ask=p_ask)
                    except Exception:
                        pass
                if quote is not None and iid in wanted:
                    loop.call_soon_threadsafe(_send_quote, quote)
                if depth_mode.get(iid, "off") != "off":
                    try:
                        depth = session.broker.depth(inst)
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
        d_position = bus.of_type(PositionUpdated).subscribe(
            lambda event: loop.call_soon_threadsafe(_send_position, event)
        )
        disposables.extend([d_quote, d_depth, d_fill, d_position])

        status_tasks.append(asyncio.create_task(_feed_status_watch()))

        while True:
            text = await ws.receive_text()
            await _handle_message(text)
    except WebSocketDisconnect:
        pass
    finally:
        if replay_run is not None:
            try:
                await _finish_run(replay_run, "replay_stopped", emit=_socket_writable())
            except Exception:
                log.exception("replay teardown failed")
        # Cancel and drain the writer so no "task was destroyed but it is
        # pending" warning leaks on loop teardown (it may be parked in
        # ``outbound.get()`` when the client leaves).
        if replay_cleanup_tasks:
            await asyncio.gather(*tuple(replay_cleanup_tasks), return_exceptions=True)
        writer_task.cancel()
        try:
            await writer_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 – teardown
            pass
        for task in status_tasks:
            task.cancel()
        for task in status_tasks:
            try:
                await task
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
