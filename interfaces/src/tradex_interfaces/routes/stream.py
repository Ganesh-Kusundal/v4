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
from tradex_domain.bar_source import BarSource
from tradex_domain.constants import (
    PAPER_SNAPSHOT_SPREAD,
    WS_INDICATOR_SUB,
    WS_INDICATOR_UNSUB,
    WS_REPLAY_PAUSE,
    WS_REPLAY_SEEK,
    WS_REPLAY_START,
    WS_SUBSCRIBE_BARS,
)
from tradex_domain.errors import CapabilityNotSupportedError
from tradex_domain.timeframe import to_timeframe
from tradex_domain.timezones import IST_ZONE
from tradex_runtime.replay_controller import (
    ReplayController,
    ReplayStartParams,
    ReplayTransitionError,
    parse_replay_start,
)

from tradex_interfaces._helpers import feed_status_payload
from tradex_interfaces.queueing import (
    CONTROL_QUEUE_MAX,
    WS_CLOSE_CONTROL_OVERFLOW,
    _enqueue_control,
    _enqueue_drop_oldest,
    outbound_queue_stats,
)
from tradex_interfaces.replay_guard import ReplayGuard
from tradex_interfaces.routes.stream_indicators import (
    IndicatorStream,
    IndicatorStreamRegistry,
)

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
    # Third lane, drained before BOTH others. Control has strict priority over
    # ticks, which is right for a fill racing a quote — but it inverts a
    # dependency the replay protocol states: a ``replay_stepped`` ack means
    # "the bar you just stepped over is on the wire". With only two lanes the
    # writer sorts the ack ahead of the bar that is still queued, so the client
    # receives the ack first and can act on a bar it has not got. A barrier
    # frame is therefore not a normal control frame: it is a control frame
    # that additionally waits for the tick queue to be empty.
    barrier: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
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
            # Barrier lane LAST, and only once the tick queue is empty. A
            # barrier frame asserts something about bars already produced
            # (``replay_stepped`` says "the bar you stepped over is out"), so
            # sending it while a bar is still queued would put the ack on the
            # wire before the fact it reports. Draining it here — after ticks,
            # and only when ticks is empty — makes the ordering true on the
            # wire rather than only inside the controller.
            if not any(priority == 1 for priority, _ in batch):
                try:
                    batch.append((2, barrier.get_nowait()))
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
                b_get = asyncio.create_task(_get_tagged(barrier, 2))
                try:
                    done, _pending = await asyncio.wait(
                        {t_get, c_get, b_get},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for fut in done:
                        batch.append(fut.result())
                finally:
                    for fut in (t_get, c_get, b_get):
                        if not fut.done():
                            fut.cancel()
                # A barrier that woke us while ticks also arrived must not
                # overtake them: put any barrier frame back and only keep
                # control/ticks for this batch.
                deferred: list[dict[str, Any]] = []
                kept: list[tuple[int, dict[str, Any]]] = []
                for priority, payload in batch:
                    if priority == 2:
                        deferred.append(payload)
                    else:
                        kept.append((priority, payload))
                if deferred and kept:
                    for payload in deferred:
                        barrier.put_nowait(payload)
                    batch = kept
                elif deferred and not kept:
                    # Barrier alone is fine only when ticks is empty — which
                    # it is, or we would have woken on ticks too and kept
                    # them. Re-check in case a race slipped a tick in.
                    if not ticks.empty():
                        for payload in deferred:
                            barrier.put_nowait(payload)
                        batch = []
                        continue
                    batch = [(2, p) for p in deferred]
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
                if (
                    frame.get("type") != "bar"
                    or frame.get("source") != BarSource.SIM.value
                    or frame.get("run_id") != run_id
                ):
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
            elif msg_type == WS_SUBSCRIBE_BARS:
                await _subscribe_bars(msg)
            elif msg_type == "unsubscribe_bars":
                _unsubscribe_bars(msg)
                _ack({"type": "unsubscribed_bars"})
            elif msg_type == WS_INDICATOR_SUB:
                _indicator_sub(msg)
            elif msg_type == WS_INDICATOR_UNSUB:
                _indicator_unsub(msg)
            elif msg_type == WS_REPLAY_START:
                await _replay_start(msg)
            elif msg_type == WS_REPLAY_PAUSE:
                await _replay_pause()
            elif msg_type == "replay_resume":
                await _replay_resume()
            elif msg_type == "replay_speed":
                await _replay_speed(msg)
            elif msg_type == "replay_step":
                await _replay_step()
            elif msg_type == WS_REPLAY_SEEK:
                await _replay_seek(msg)
            elif msg_type == "replay_stop":
                await _replay_stop()
            elif msg_type == "stats":
                # Surface backpressure + resync health so a client can
                # observe tick drops, control pressure, and whether it
                # must reconnect for a fresh OMS snapshot.
                _ack(outbound_queue_stats(
                    dropped=dropped[0],
                    queued_ticks=ticks.qsize(),
                    queued_control=control.qsize(),
                    queued_barrier=barrier.qsize(),
                    tick_cap=outbound_max,
                    control_cap=CONTROL_QUEUE_MAX,
                    resync_required=control_overflow[0],
                ))
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

        def _normalize_ws_interval(interval: str) -> str:
            """REST daily is ``D``; domain Timeframe / WS is ``1d``.

            Both spellings go through the one domain path —
            :func:`to_timeframe` resolves every daily alias onto the
            canonical Timeframe, whose ``.value`` is the wire form. Total
            on purpose: an unresolvable interval passes through unchanged
            so the refusal still happens where the aggregator is built
            (inside the subscribe try), exactly as when this was a pure
            string mapping.
            """
            try:
                return to_timeframe(interval).value
            except ValueError:
                return interval

        def _send_bar_frame(frame: Any) -> None:
            # Replay bars deliberately do NOT come through here: the
            # controller frames its own SIM bars so it can track its own
            # bucket closure. This path is live bars only.
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
            if (
                getattr(frame, "closed", False) is True
                and getattr(frame, "source", "live") == "live"
            ):
                _mf = getattr(session, "market_feed", None)
                if _mf is not None:
                    try:
                        _mf.on_bar_frame(frame)
                    except Exception:
                        log.exception("on_bar_frame integrity hook failed for %s", frame.instrument)

        # --- Indicator push (Tier-2 subscribe seam) ----------------------
        # One registry per connection (subscriptions are per-connection), but
        # the datalake tail and warm indicator state behind it are
        # process-level, so a second client on the same series reuses them.
        indicator_registry = IndicatorStreamRegistry(
            lambda frame_dict: _track_drop(_enqueue_drop_oldest(ticks, frame_dict))
        )
        indicator_stream = IndicatorStream(indicator_registry, _ack)

        def _indicator_sub(msg: dict[str, Any]) -> None:
            indicator_stream.sub(msg)

        def _indicator_unsub(msg: dict[str, Any]) -> None:
            indicator_stream.unsub(msg)
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

        # Replay orchestration lives in the runtime (ReplayController); this
        # closure is transport only -- it owns the socket and the queues, not
        # the replay lifecycle. Lifecycle frames go to the CONTROL queue, which
        # is never evicted: a dropped replay_stopped would leave the client
        # believing a run ended when it did not. Bar frames stay on the lossy
        # tick queue. Frames that assert something about bars already produced
        # go through the barrier lane instead, so they cannot overtake a bar
        # that is still queued.
        replay_cleanup_tasks: set[asyncio.Task[Any]] = set()

        def _replay_control(frame: dict[str, Any]) -> None:
            if not _enqueue_control(control, frame) and not control_overflow[0]:
                control_overflow[0] = True
                _track_drop(1)
                log.error(
                    "replay control frame dropped (cap=%d) type=%s; closing "
                    "client to force resync",
                    CONTROL_QUEUE_MAX,
                    frame.get("type"),
                )

        def _replay_barrier(frame: dict[str, Any]) -> None:
            """Enqueue a lifecycle frame that must not overtake queued bars.

            Used for ``replay_stepped`` and the ``replay_paused`` that follows
            it. The writer will not send one until the tick queue is empty,
            which is what makes "the bar you stepped over has arrived" true on
            the wire rather than only inside the controller.
            """
            barrier.put_nowait(frame)

        async def _await_ticks_idle() -> None:
            """Block until the tick queue is empty (writer has drained bars).

            Bound so a wedged writer cannot hang a step forever.
            """
            for _ in range(200):
                if ticks.empty():
                    return
                await asyncio.sleep(0.01)
            log.warning("tick queue still non-empty after step drain wait")


        def _replay_error(message: str, run_id: str | None = None) -> None:
            frame: dict[str, Any] = {
                "type": "replay_error",
                "message": str(message),
            }
            if run_id is not None:
                frame["run_id"] = run_id
            dropped[0] += _track_drop(_enqueue_drop_oldest(ticks, frame))

        def _replay_error(message: str, run_id: str | None = None) -> None:
            frame: dict[str, Any] = {
                "type": "replay_error",
                "message": str(message),
            }
            if run_id is not None:
                frame["run_id"] = run_id
            _replay_control(frame)

        def _send_replay_bar(frame: dict[str, Any]) -> None:
            """Replay bars stay on the lossy tick queue (market-data policy)."""
            dropped[0] += _track_drop(_enqueue_drop_oldest(ticks, frame))

        def _replay_aggregator(
            instrument: str, interval: str, run_id: str, on_frame: Any
        ) -> Any:
            """Replay bars are SIM-sourced, so they get their own aggregator.

            Deliberately not the live ``_make_aggregator``: that one stamps
            ``source="live"``, and the live missing-bar integrity tracker would
            treat a synthetic tick path as real market data.
            """
            from tradex_domain.enums import Timeframe
            from tradex_runtime.bar_aggregator import BarAggregator

            return BarAggregator(
                instrument,
                Timeframe(_normalize_ws_interval(interval)),
                on_frame=on_frame,
                run_id=run_id,
            )

        def _load_replay_candles(params: ReplayStartParams) -> tuple[Any, ...]:
            """Resolve the replay dataset via the shared MarketWindowLoader.

            Same resolution path as chart history/backtest, so a window that
            one path sees the other path sees — the data gate's whole point.

            The store is constructed here (not inside the loader's default)
            so a test that monkeypatches ``ParquetStorage`` still reaches the
            fake: ``ParquetMarketProvider`` binds the class at import time.
            """
            from tradex_domain.enums import Timeframe
            from tradex_domain.instruments import Equity
            from tradex_market_data.market_provider import ParquetMarketProvider
            from tradex_market_data.parquet_storage import ParquetStorage
            from tradex_market_data.paths import DATALAKE_ROOT
            from tradex_market_data.window_loader import MarketSource, MarketWindowLoader

            exchange, symbol = params.instrument.split(":", 1)
            equity = Equity.of(exchange, symbol)
            store = ParquetStorage(DATALAKE_ROOT, metrics=getattr(session, "metrics", None))
            loader = MarketWindowLoader(
                provider=ParquetMarketProvider(store=store),
            )
            window = loader.load_replay_m1(
                equity,
                minutes=params.minutes,
                start_time=params.start_time,
            )
            if window.source is MarketSource.NONE or not window.candles:
                raise ValueError(
                    f"no datalake history in last {params.minutes}m for {symbol}"
                    if params.start_time is None
                    else f"no datalake history for {symbol}"
                )
            if any(candle.timeframe != Timeframe.M1 for candle in window.candles):
                raise ValueError("replay dataset must contain M1 candles")
            return window.candles

        async def _replay_start(
            msg: dict[str, Any], dataset: tuple[Any, ...] | None = None
        ) -> None:
            try:
                params = parse_replay_start(msg)
            except ValueError as exc:
                _replay_error(str(exc))
                return
            try:
                await replay_controller.start(params, dataset=dataset)
            except ValueError as exc:
                # A client-caused refusal (no datalake history, empty or
                # non-M1 dataset). The controller has already emitted the
                # ``replay_error`` frame; nothing more to do here.
                log.debug("replay_start refused for %s: %s", params.instrument, exc)
            except ReplayTransitionError as exc:
                _replay_error(str(exc), replay_controller.run_id)
            except RuntimeError as exc:
                # Guard contention: another connection holds the one replay
                # slot process-wide. Also a client-visible condition.
                _replay_error(str(exc))
            except Exception:
                # A defect in this process, not a client problem. Log it loudly
                # and re-raise so it is not laundered into a "replay did not
                # start" reply: catching Exception here hid a NameError in
                # this module behind that reply for a whole debugging pass.
                log.exception("replay_start failed for %s", params.instrument)
                raise

        async def _replay_pause() -> None:
            try:
                await replay_controller.pause()
            except ReplayTransitionError as exc:
                _replay_error(str(exc), replay_controller.run_id)

        async def _replay_resume() -> None:
            try:
                await replay_controller.resume()
            except ReplayTransitionError as exc:
                _replay_error(str(exc), replay_controller.run_id)

        async def _replay_step() -> None:
            try:
                await replay_controller.step()
            except ReplayTransitionError as exc:
                _replay_error(str(exc), replay_controller.run_id)

        async def _replay_seek(msg: dict[str, Any]) -> None:
            try:
                await replay_controller.seek(msg)
            except ReplayTransitionError as exc:
                _replay_error(str(exc), replay_controller.run_id)

        async def _replay_speed(msg: dict[str, Any]) -> None:
            try:
                replay_controller.set_speed(msg.get("speed"))
            except (ReplayTransitionError, ValueError) as exc:
                _replay_error(str(exc), replay_controller.run_id)
                await replay_controller.stop()

        async def _replay_stop(silent: bool = False) -> None:
            await replay_controller.stop()

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
                        from tradex_market_data.window_loader import MarketWindowLoader

                        store = ParquetStorage(
                            DATALAKE_ROOT, metrics=getattr(session, "metrics", None)
                        )
                        # Use MarketWindowLoader as the canonical data loading entry point.
                        loader = MarketWindowLoader(
                            provider=ParquetMarketProvider(store=store)
                        )
                        sym = getattr(inst, "symbol", None) or str(iid).split(":")[-1]
                        rng = store.date_range(sym)
                        if rng and rng[1]:
                            series = loader.history(
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
                                p_bid = Price(last_c - PAPER_SNAPSHOT_SPREAD)
                                p_ask = Price(last_c + PAPER_SNAPSHOT_SPREAD)
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

        # Built here, not at the top of the closure, so every adapter it
        # references is already bound. Constructing it earlier made the
        # handler raise UnboundLocalError on the first frame — which the
        # TestClient surfaces as a socket that closed before any ack.
        replay_controller = ReplayController(
            make_aggregator=_replay_aggregator,
            emit_control=_replay_control,
            emit_bar=_send_replay_bar,
            emit_barrier=_replay_barrier,
            await_ticks_idle=_await_ticks_idle,
            load_candles=_load_replay_candles,
            on_bar_frame=lambda frame: indicator_registry.handle_bar_frame(frame),
            guard=replay_guard,
            purge_run_frames=_drop_replay_frames,
        )

        status_tasks.append(asyncio.create_task(_feed_status_watch()))

        while True:
            text = await ws.receive_text()
            await _handle_message(text)
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await replay_controller.close()
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
        # Give the process-level indicator tail/series caches their end of
        # life. The registry holds reference counts on them, so without this
        # a series' warm window would outlive every connection that ever
        # subscribed to it.
        try:
            indicator_registry.close()
        except Exception:  # pragma: no cover – defensive
            log.exception("indicator registry close failed")
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
