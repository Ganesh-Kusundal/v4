"""Reactive strategy engine — strategies subscribe to typed Observable streams."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from tradex_domain.enums import OrderStatus, OrderType, TimeInForce
from tradex_domain.events import ErrorOccurred, OrderCancelled, OrderFilled
from tradex_domain.execution import Order, OrderReceipt, OrderRequest, Position
from tradex_domain.instruments import InstrumentId
from tradex_domain.market import Candle, Depth, Quote
from tradex_domain.strategy import Signal, StrategyContext
from tradex_domain.value_objects import CorrelationId, Money, OrderId, Price, Quantity

from tradex_strategy.core.decision_pipeline import DecisionPipeline
from tradex_strategy.core.routing import (
    StrategySubscription,
    SubscriptionRouter,
    event_instrument_id,
)
from tradex_strategy.core.sizing import PositionSizer, unconfigured_sizer
from tradex_strategy.core.views import MarketView, PortfolioView

if TYPE_CHECKING:
    from tradex_analytics.runtime import AnalyticsRuntime

log = logging.getLogger(__name__)

_FILL_REFERENCES = frozenset({"next_open", "signal_close"})

#: Order statuses a stop does NOT touch (the order is settled). Mirrors the
#: kill switch's terminal set and deliberately EXCLUDES ``UNKNOWN``: an
#: UNKNOWN order crossed the venue boundary and may still be live, so a
#: strategy stop must reach it.
_SETTLED_STATUSES = frozenset({
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
})


class ReactiveStrategyEngine:
    """Strategies subscribe to typed Observable streams via the bus.

    ``fill_reference`` controls when signal-triggered orders are submitted:

    - ``"next_open"`` (default): orders are held until the next Candle of the
      same instrument and filled at that bar's OPEN — the same model as
      BacktestEngine, so backtest, replay, paper, and live fill identically
      (parity review CRITICAL-1). Quote/depth signals defer to the next candle
      open too, exactly like BacktestEngine's timestamp-matched fills.
    - ``"signal_close"``: orders are submitted immediately, priced at the
      triggering event (candle close / quote LTP / depth mid) — for live
      quote-driven strategies that must act without waiting for a candle.

    ``sizer`` is the ``PositionSizer`` that turns each signal into an order
    quantity. ``Signal.strength`` is signal quality and is never read as a
    quantity here — that one-liner, ``abs(signal.strength)``, is what made
    every strategy in this repository buy exactly one share, because every
    strategy emits ``strength=1.0``. Omitting ``sizer`` falls back to
    :func:`~tradex_strategy.core.sizing.unconfigured_sizer`, which logs a
    WARNING and sizes to a fixed notional.

    **Orders leave through ``submit``, never the bus (AC-22).** ``submit`` is the
    engine's own ``ExecutionEngine.submit``, injected rather than imported so
    this module keeps no dependency on the execution layer and a test can hand
    in a recorder. It is a REQUIRED argument: there is deliberately no default,
    because the previous default (publish onto whatever bus was passed) is the
    door AC-22 closed. A strategy's signal is sized here, gets its protective
    levels here, and is then handed to this callable — so the ``PositionSizer``
    and ``protective_request`` gates sit in front of the only path to the
    venue, rather than being bypassable by anyone holding a bus reference.
    """

    def __init__(
        self,
        bus,
        submit: Callable[[OrderRequest], OrderReceipt] | None = None,
        *,
        fill_reference: str = "next_open",
        pending_max_age_bars: int = 0,
        sizer: PositionSizer | None = None,
        analytics: AnalyticsRuntime | None = None,
        positions_provider: Callable[[], Iterable[Position]] | None = None,
        balance_provider: Callable[[], Money] | None = None,
    ) -> None:
        """Initialize with a ReactiveBus.

        Args:
            bus: ReactiveBus instance for event streaming (observations in,
                lifecycle events out — NOT an order-intake surface).
            submit: callable that takes a fully-formed ``OrderRequest`` to the
                venue, i.e. ``ExecutionEngine.submit``. REQUIRED in every real
                wiring; it may only be ``None`` in a unit test that asserts on
                the *requests* this engine builds and never lets one reach a
                fill source. A ``None`` submit means orders are built and
                dropped, which is logged at ERROR so it cannot masquerade as a
                working strategy in a live session.
            fill_reference: ``"next_open"`` (default) or ``"signal_close"`` —
                see the class docstring.
            pending_max_age_bars: maximum number of bars a deferred
                ``next_open`` order may sit in the queue before the engine
                cancels it with ``OrderCancelled`` (parity review G5). ``0``
                disables expiry (backward-compatible). Tracking counts the
                number of bars processed since the order was deferred, so a
                value of ``N`` cancels the order on the ``N+1``-th bar that
                does not match its instrument.
            sizer: the ``PositionSizer`` for this engine. ``None`` means
                :func:`~tradex_strategy.core.sizing.unconfigured_sizer`.
            analytics: optional ``AnalyticsRuntime`` each candle is ingested
                into before the strategy is evaluated; the resulting
                ``AnalyticsView`` for the candle's (instrument, timeframe) is
                passed to the strategy via ``StrategyContext.analytics``.
                ``None`` (default) leaves the context field ``None``.
            positions_provider: optional zero-arg callable returning the open
                positions (e.g. ``cache.all_positions``). When wired, the
                context carries the read-only ``position``/``portfolio`` views
                resolved for the event's instrument. ``None`` (default)
                leaves those fields ``None``.
            balance_provider: optional zero-arg callable returning the account
                balance as ``Money`` (e.g. ``lambda: engine.cash_snapshot().cash``).
                Feeds the ``portfolio`` view's ``balance``.
        """
        if fill_reference not in _FILL_REFERENCES:
            raise ValueError(f"unknown fill_reference: {fill_reference!r}")
        if pending_max_age_bars < 0:
            raise ValueError(
                f"pending_max_age_bars must be >= 0, got {pending_max_age_bars!r}"
            )
        if submit is None:
            log.error(
                "ReactiveStrategyEngine constructed with submit=None: orders "
                "will be built and dropped, never reaching a venue. This is only "
                "valid in a unit test that inspects the requests.",
            )
        self._submit = submit
        self._fill_reference = fill_reference
        self._bus = bus
        self._sizer = sizer if sizer is not None else unconfigured_sizer()
        self._pipeline = DecisionPipeline(self._sizer)
        self._analytics = analytics
        self._strategies: dict[str, Any] = {}
        # Per-symbol event routing (Phase 1). Built at register() from
        # each strategy's declared instruments; consulted by the wrapper
        # handlers before a strategy is evaluated.
        self._router = SubscriptionRouter()
        # Per-strategy subscriptions (AC-8). Keyed by strategy_id so a
        # single strategy can be stopped/unregistered without touching any
        # other strategy's delivery — RxPY disposables are independent, so
        # disposing one strategy's observers cannot silence another's.
        self._disposables: dict[str, list] = {}
        # Strategy ids whose event delivery is gated (PAUSED or STOPPED).
        # Checked by every wrapper handler; PAUSED keeps its subscriptions
        # alive so resume() re-attaches without re-registering.
        self._gated: set[str] = set()
        self._bar_count = 0
        # Per-signal nonce for deterministic correlation ids (seeded per
        # engine instance, so ids are stable within a replay run).
        self._signal_seq = 0
        # Deferred signal orders awaiting the next candle of their instrument
        # (next_open model). Indexed by ``InstrumentId`` so per-bar flushes
        # and age sweeps run in O(k) for *k* pending on that instrument
        # rather than O(n) over the whole queue (G5). Each entry carries
        # ``{strategy_id, strategy_version, instrument_id, signal, quantity,
        # correlation_id, deferred_at_bar}`` where ``deferred_at_bar`` is
        # the engine ``_bar_count`` at the time the signal was deferred
        # (used by the max-age sweep).
        self._pending: dict[InstrumentId, list[dict]] = {}
        # 0 == no expiry (backward-compat). > 0 == cancel after N bars.
        self._pending_max_age_bars = pending_max_age_bars
        # F-47: per-strategy order outcomes. Live publishes ErrorOccurred and
        # bumps a session-global counter, so a strategy whose orders are
        # refused reads the same as one that is fine; the backtest re-raises
        # and aborts, so it never had this gap. These make the live failure
        # at least as nameable as the backtest one.
        self._order_failures: dict[str, int] = {}
        self._orders_expired: dict[str, int] = {}
        # Read-only context views (Phase 6-7). ``_latest_quotes`` is the
        # engine's own store, updated as Quote events arrive; each context
        # gets a MarketView — a live, read-only (write-rejecting) proxy over
        # that store, not a point-in-time snapshot, so a strategy holding a
        # context still sees quotes that arrived after the fact.
        self._latest_quotes: dict[InstrumentId, Quote] = {}
        self._positions_provider = positions_provider
        self._balance_provider = balance_provider

    def _make_context(self, **overrides: Any) -> StrategyContext:
        """Create a StrategyContext with current engine state.

        Carries the read-only ``market`` view (a live proxy over the latest
        quotes, not a snapshot) on every call. When ``instrument`` is passed
        — every event handler passes its own event's instrument — the
        context also carries the ``position``/``portfolio`` views resolved
        for that instrument from the wired providers. Explicit ``position``/
        ``portfolio``/``market`` overrides win over the computed views.
        """
        instrument = overrides.pop("instrument", None)
        views: dict[str, Any] = {"market": MarketView(self._latest_quotes)}
        if instrument is not None:
            views["position"] = self._position_for(instrument)
            views["portfolio"] = self._portfolio_view()
        # Merge rather than ``StrategyContext(**views, **overrides)``: an
        # override naming a computed key (market, or position/portfolio when
        # an instrument was passed) would raise ``TypeError: got multiple
        # values for keyword argument`` instead of winning.
        merged = {**views, **overrides}
        return StrategyContext(**merged)

    def _position_for(self, instrument: Any) -> Position | None:
        """Open position for *instrument* from the wired provider, or None."""
        if self._positions_provider is None:
            return None
        inst_id = getattr(instrument, "instrument_id", instrument)
        for position in self._positions_provider():
            if position.instrument.instrument_id == inst_id:
                return position
        return None

    def _portfolio_view(self) -> PortfolioView | None:
        """Balance + positions snapshot, or None when neither provider is wired."""
        if self._positions_provider is None and self._balance_provider is None:
            return None
        balance = self._balance_provider() if self._balance_provider is not None else None
        positions = (
            self._positions_provider() if self._positions_provider is not None else ()
        )
        return PortfolioView(balance=balance, positions=positions)

    @staticmethod
    def _reference_price(event: Any) -> Price | None:
        """Reference price of the event that triggered a signal.

        Candle → close; Quote → LTP; Depth → mid of best bid/ask.
        ``None`` when the event carries no price (engine refuses to build a
        price-less order from it).
        """
        if hasattr(event, "ohlc") and hasattr(event.ohlc, "close"):
            return Price(value=Decimal(str(event.ohlc.close.value)))
        if hasattr(event, "ltp"):
            return Price(value=Decimal(str(event.ltp.value)))
        if hasattr(event, "bids") and hasattr(event, "asks") and event.bids and event.asks:
            mid = (event.bids[0][0].value + event.asks[0][0].value) / 2
            return Price(value=Decimal(str(mid)))
        return None

    def _correlation_id(self, strategy_id: str, event: Any) -> CorrelationId:
        """Deterministic correlation id for a signal-triggered order.

        Derived from strategy id + event identity (timestamp + type) plus a
        per-signal nonce, so the same replay stream reproduces identical ids
        (idempotency-safe), distinct events never collide, and two signals
        emitted from one event still get distinct ids.
        """
        self._signal_seq += 1
        ts = getattr(event, "timestamp", None)
        seed = f"{strategy_id}:{type(event).__name__}:{ts}:{self._signal_seq}"
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
        return CorrelationId(value=f"strat-{digest}")

    def _wrap_on_bar(self, strategy: Any) -> Any:
        """Wrap strategy.on_bar to inject context and bridge Signal→Order."""
        def handler(candle: Any) -> Any:
            if strategy.strategy_id in self._gated:
                return None
            # Increment first so ``deferred_at_bar`` (set when a signal is
            # deferred) and the max-age sweep (which computes
            # ``_bar_count - deferred_at_bar``) share a consistent index
            # of the bar being processed. A deferred signal from bar N
            # thus ages 1 on bar N+1, 2 on bar N+2, ...
            self._bar_count += 1
            if self._fill_reference == "next_open":
                # Fill prior deferred orders at THIS bar's open, then let the
                # strategy evaluate the bar (its own signal waits for the next).
                self._flush_pending(candle)
            # Routing guard (Phase 1): skip the strategy call for
            # instruments it is not subscribed to. Placed AFTER the
            # bookkeeping above so _bar_count and _flush_pending evolve
            # exactly as before — only the strategy evaluation is skipped.
            inst_id = event_instrument_id(candle)
            if inst_id is not None and not self._router.delivers(
                strategy.strategy_id, Candle, inst_id
            ):
                return None
            # Feed the canonical bar store and bind the view to this
            # candle's (instrument, timeframe). The strategy reads indicator
            # values from the shared view instead of computing locally.
            view = None
            if self._analytics is not None:
                self._analytics.ingest(candle)
                view = self._analytics.view(candle.instrument.instrument_id, candle.timeframe)
            ctx = self._make_context(
                bar_count=self._bar_count,
                timestamp=getattr(candle, "timestamp", None),
                analytics=view,
                instrument=getattr(candle, "instrument", None),
            )
            result = strategy.on_bar(ctx, candle)
            self._maybe_publish_order(result, strategy, candle)
            return result
        return handler

    def pending_snapshot(self) -> tuple[dict, ...]:
        """Read-only view of deferred next_open orders (backtest bridge) [REF-5].

        Public seam replacing the former ``strategy_engine._pending`` reach-in
        from ``replay/backtest.py``. Returns a tuple of the per-instrument
        lists flattened into a single sequence so existing callers
        (``for pending in engine.pending_snapshot()``) are unaffected by
        the G5 dict-by-instrument refactor.
        """
        return tuple(
            entry
            for entries in self._pending.values()
            for entry in entries
        )

    def flush_pending(self, candle: Any) -> None:
        """Public flush of deferred orders for *candle* (replay drivers) [REF-5]."""
        self._flush_pending(candle)

    def _place(self, request: OrderRequest, strategy_id: str) -> None:
        """Hand *request* to the venue through the injected ``submit`` (AC-22).

        Every order this engine produces goes through here, so the sizer and
        the protective-pair gate are unconditionally in front of the single
        path to the venue. An engine built with ``submit=None`` logs and drops
        the order rather than publishing it anywhere: a dropped order is
        visible, whereas a stray one on a shared bus is invisible.

        A ``submit()`` failure is republished as ``ErrorOccurred`` rather than
        re-raised. That is not politeness, it is the exact contract this
        engine had when the order travelled as a bus command: ``ReactiveBus``
        isolates a subscriber's exception, so the engine published
        ``ErrorOccurred`` on the caller's behalf and the raise was consumed at
        the bus. Consumers that need to see the failure — the backtest's
        ``run()`` re-raises the first one out of the run rather than reporting
        a flattering equity curve — subscribe to that event. Swallowing the
        error here instead would silently change every such contract, and an
        unaffordable order in a backtest would look like a quiet one.
        """
        if self._submit is None:
            log.error(
                "%s: dropped order for %s — no submit() wired "
                "(ReactiveStrategyEngine cannot reach a venue)",
                strategy_id, request.instrument.instrument_id,
            )
            self._order_failures[strategy_id] = (
                self._order_failures.get(strategy_id, 0) + 1
            )
            return
        try:
            self._submit(request)
        except Exception as exc:  # noqa: BLE001 — surfaced as ErrorOccurred
            log.error(
                "%s: submit() raised for %s: %s",
                strategy_id, request.instrument.instrument_id, exc,
            )
            self._order_failures[strategy_id] = (
                self._order_failures.get(strategy_id, 0) + 1
            )
            self._bus.publish(ErrorOccurred(error=exc))

    def _flush_pending(self, candle: Any) -> None:
        """Fill deferred orders for *candle*'s instrument at the candle's OPEN.

        Mirrors BacktestEngine: a signal on bar N is filled at bar N+1's open.
        Pending orders for other instruments stay queued, and each remaining
        order has its ``bar_age`` incremented (the max-age sweep runs on the
        next bar regardless of which instrument it is for).
        """
        if not self._pending:
            return
        inst_id = candle.instrument.instrument_id
        # O(k) lookup: only this instrument's deferred orders.
        pending_for_inst = self._pending.pop(inst_id, None)
        if pending_for_inst:
            open_price = Price(value=Decimal(str(candle.ohlc.open.value)))
            for pending in pending_for_inst:
                version = pending.get("strategy_version", "1.0.0")
                # Re-size against the open this order actually fills at. The
                # provisional quantity on the pending entry was taken against
                # the signal's own price, and a size is a function of price:
                # sizing a next_open order at the signal bar's close sets both
                # the notional ceiling and the risk budget against a price the
                # order never got.
                # Declared protective levels ride the deferred signal onto the
                # order, validated against the open this order actually fills
                # at — not the close the strategy saw when it declared them.
                request = self._pipeline.build_request(
                    pending["signal"], open_price, pending["strategy_id"],
                    correlation_id=pending["correlation_id"],
                    tag=f"{pending['strategy_id']}@{version}",
                    reference_timestamp=getattr(candle, "timestamp", None),
                )
                if request is None:
                    continue
                self._place(request, pending["strategy_id"])
        # Sweep stale orders from every other instrument's queue (O(n_bars)
        # instruments × O(k) per-instrument pending).
        self._sweep_stale_pending()

    def _sweep_stale_pending(self) -> None:
        """Cancel deferred orders older than ``pending_max_age_bars``.

        No-op when ``pending_max_age_bars == 0`` (backward-compat).
        Builds a synthetic ``Order`` (status CANCELLED) from the deferred
        signal because the order never reached the broker — the engine
        owns its lifecycle from deferral to fill/cancel.
        """
        if self._pending_max_age_bars <= 0 or not self._pending:
            return
        max_age = self._pending_max_age_bars
        # Iterate over a snapshot of keys to allow mutation of self._pending.
        # ``pending_max_age_bars=N`` means the order survives N bars and is
        # cancelled on bar N+1, so the test fires when ``age > N``.
        for inst_id in list(self._pending.keys()):
            entries = self._pending[inst_id]
            survivors: list[dict] = []
            for pending in entries:
                age = self._bar_count - pending["deferred_at_bar"]
                if age > max_age:
                    self._cancel_stale(pending)
                else:
                    survivors.append(pending)
            if survivors:
                self._pending[inst_id] = survivors
            else:
                del self._pending[inst_id]

    def _cancel_stale(self, pending: dict) -> None:
        """Publish ``OrderCancelled`` for a deferred order past its max age.

        The order never reached the broker (it was deferred in the
        ``next_open`` queue), so we synthesize a CANCELLED ``Order`` from
        the signal metadata. ``OrderId`` is derived from the correlation
        id so audit trails can still match it to the strategy signal.
        """
        version = pending.get("strategy_version", "1.0.0")
        signal: Signal = pending["signal"]
        correlation_id: CorrelationId | None = pending.get("correlation_id")
        order_id = OrderId(
            value=(
                f"pending-{correlation_id.value}"
                if correlation_id is not None
                else "pending-stale"
            )
        )
        order = Order(
            order_id=order_id,
            instrument=signal.instrument,
            side=signal.direction,
            order_type=OrderType.MARKET,
            quantity=pending["quantity"],
            price=None,
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.CANCELLED,
            correlation_id=correlation_id,
            tag=f"{pending['strategy_id']}@{version}",
            strategy_id=pending["strategy_id"],
        )
        log.warning(
            "strategy %s deferred order on %s cancelled after %d bars "
            "(pending_max_age_bars=%d); strategy may be signalling on a "
            "delisted or halted instrument",
            pending["strategy_id"],
            signal.instrument.instrument_id.underlying,
            self._bar_count - pending["deferred_at_bar"],
            self._pending_max_age_bars,
        )
        # F-45: count it, so live has the equivalent of the backtest's
        # ``num_unfilled``. The backtest reports deferred orders that never
        # filled; without this number a live session cannot be compared to one.
        _sid = str(pending["strategy_id"])
        self._orders_expired[_sid] = self._orders_expired.get(_sid, 0) + 1
        self._bus.publish(OrderCancelled(order=order))

    def _wrap_on_quote(self, strategy: Any) -> Any:
        """Wrap strategy.on_quote to inject context and bridge Signal→Order."""
        def handler(quote: Any) -> Any:
            # Track the latest quote per instrument: the market view a
            # strategy reads is a snapshot of this store. Tracked before
            # the gates below so the store is complete regardless of which
            # strategies are subscribed, paused, or stopped. A test fake
            # with no instrument cannot be keyed, so it is simply not tracked.
            inst_id = event_instrument_id(quote)
            if inst_id is not None:
                self._latest_quotes[inst_id] = quote
            if strategy.strategy_id in self._gated:
                return None
            # Routing guard (Phase 1): skip the strategy call for
            # instruments it is not subscribed to.
            if inst_id is not None and not self._router.delivers(
                strategy.strategy_id, Quote, inst_id
            ):
                return None
            ctx = self._make_context(
                timestamp=getattr(quote, "timestamp", None),
                instrument=getattr(quote, "instrument", None),
            )
            result = strategy.on_quote(ctx, quote)
            self._maybe_publish_order(result, strategy, quote)
            return result
        return handler

    def _wrap_on_depth(self, strategy: Any) -> Any:
        """Wrap strategy.on_depth to inject context."""
        def handler(depth: Any) -> Any:
            if strategy.strategy_id in self._gated:
                return None
            # Routing guard (Phase 1): skip the strategy call for
            # instruments it is not subscribed to.
            inst_id = event_instrument_id(depth)
            if inst_id is not None and not self._router.delivers(
                strategy.strategy_id, Depth, inst_id
            ):
                return None
            ctx = self._make_context(
                timestamp=getattr(depth, "timestamp", None),
                instrument=getattr(depth, "instrument", None),
            )
            if hasattr(strategy, "on_depth"):
                result = strategy.on_depth(ctx, depth)
                self._maybe_publish_order(result, strategy, depth)
                return result
        return handler

    def _wrap_on_fill(self, strategy: Any) -> Any:
        """Wrap strategy.on_fill to inject context."""
        def handler(event: Any) -> Any:
            if strategy.strategy_id in self._gated:
                return None
            fill = event.fill
            # Fills route by owning strategy, not instrument. A fill with
            # no strategy_id (manual/legacy) goes to all strategies.
            if fill.strategy_id is not None and fill.strategy_id != strategy.strategy_id:
                return None
            ctx = self._make_context(instrument=getattr(fill, "instrument", None))
            return strategy.on_fill(ctx, fill)
        return handler

    def _maybe_publish_order(
        self, result: Any, strategy: Any, event: Any = None,
    ) -> None:
        """If *result* is a Signal, build a sized order and submit it (AC-22).

        The order is stamped with a deterministic correlation_id (derived from
        the triggering event). In ``signal_close`` mode it is priced at the
        triggering event's reference price; in ``next_open`` mode it is
        deferred and filled at the next candle's open, so fill sources never
        receive a price-less MARKET order and every mode fills identically.

        The strategy's ``version`` is stamped onto the signal metadata AND the
        order tag (``strategy_id@version``) so the audit trail identifies
        exactly which strategy version produced each order (parity review
        area #5: strategies are versioned artifacts).
        """
        if not isinstance(result, Signal):
            return
        strategy_id = strategy.strategy_id
        version = str(getattr(strategy, "version", "1.0.0"))
        result.metadata.setdefault("strategy_version", version)
        correlation_id = self._correlation_id(strategy_id, event)
        tag = f"{strategy_id}@{version}"
        if self._fill_reference == "next_open":
            # Defer: fill at the next Candle of this instrument (its open),
            # mirroring BacktestEngine. Never fires without a following bar —
            # the same skip BacktestEngine applies to unmatched signals.
            # The pending queue is keyed by ``InstrumentId`` (G5) so per-bar
            # flushes and max-age sweeps stay O(k) per instrument. The
            # ``deferred_at_bar`` field is the engine ``_bar_count`` at the
            # moment of deferral, used by the max-age sweep.
            #
            # The quantity here is PROVISIONAL, taken against the price the
            # signal was seen at. ``_flush_pending`` re-sizes authoritatively
            # against the open this order actually fills at, because both the
            # notional ceiling and the risk budget are prices: sized against
            # the signal bar's close, a ``next_open`` order is sized against a
            # price it never got.
            inst_id = result.instrument.instrument_id
            provisional = self._size(result, self._reference_price(event), strategy_id)
            if provisional is None:
                return
            self._pending.setdefault(inst_id, []).append({
                "strategy_id": strategy_id,
                "strategy_version": version,
                "instrument_id": inst_id,
                "signal": result,
                "quantity": provisional,
                "correlation_id": correlation_id,
                "deferred_at_bar": self._bar_count,
            })
            # The deferred leg mints its CorrelationId here too, so the chain
            # point is emitted on this path as well — ``next_open`` is the
            # DEFAULT fill reference, and a chain that only held for
            # signal_close would be absent from nearly every real run.
            log.info(
                "trace signal.emit instrument=%s",
                inst_id,
                extra={
                    "stage": "signal.emit",
                    "instrument": inst_id,
                    "strategy_id": strategy_id,
                    "correlation_id": str(correlation_id),
                },
            )
            return
        price = self._reference_price(event)
        if price is None:
            raise ValueError(
                f"strategy {strategy_id} emitted {result.reason!r} without a "
                "priced event (candle/quote/depth) to reference"
            )
        request = self._pipeline.build_request(
            result, price, strategy_id,
            correlation_id=correlation_id,
            tag=tag,
            reference_timestamp=getattr(event, "timestamp", None),
        )
        if request is None:
            return
        # AC-17 / lane 5c, second link in the chain. This is where the id is
        # born: the strategy derives it from the triggering event and stamps it
        # on the order, so logging it here is what lets an operator find the
        # order that a given tick produced. The id is attached as a structured
        # field (extra) rather than interpolated into the message text.
        #
        # This is the same CorrelationId the order is persisted under — not a
        # parallel trace_id — so one grep connects the signal to the rows in
        # the orders and idempotency tables.
        log.info(
            "trace signal.emit instrument=%s",
            result.instrument.instrument_id,
            extra={
                "stage": "signal.emit",
                "instrument": result.instrument.instrument_id,
                "strategy_id": strategy_id,
                "correlation_id": str(correlation_id),
            },
        )
        self._place(request, strategy_id)

    def _size(
        self, signal: Signal, price: Price | None, strategy_id: str,
    ) -> Quantity | None:
        """Ask the sizer for this signal's size, loudly if it cannot decide.

        ``None`` means **refused**: the sizer had no knowable denominator and
        the engine drops the signal rather than publishing an order it cannot
        justify. The reason is logged at ERROR with the strategy named, so a
        dropped signal is never a quiet one.
        """
        if price is None:
            log.error(
                "%s: dropped %s — the signal has no priced event to size against",
                strategy_id, getattr(signal, "reason", ""),
            )
            return None
        decision = self._sizer.size(signal, reference_price=price)
        if decision.quantity is None:
            log.error(
                "%s: refused to size %s — %s",
                strategy_id, getattr(signal, "reason", ""), decision.reason,
            )
            return None
        log.info(
            "%s: sized %s to %s (%s)", strategy_id,
            getattr(signal, "reason", ""), decision.quantity, decision.reason,
        )
        return decision.quantity

    def register(self, strategy) -> None:
        """Register a strategy and subscribe it to market events.

        Args:
            strategy: Strategy instance implementing the Strategy protocol
        """
        self._strategies[strategy.strategy_id] = strategy

        from tradex_domain import Candle, Quote
        from tradex_domain.market import Depth

        # Build the routing index. Strategies declaring instruments are
        # routed per instrument; those declaring none (portfolio strategies
        # that trade everything) get a wildcard subscription.
        instruments = getattr(strategy, "instruments", None)
        if instruments:
            for inst in instruments:
                inst_id = getattr(inst, "instrument_id", inst)
                self._router.subscribe(
                    StrategySubscription(Candle, inst_id), strategy.strategy_id
                )
                self._router.subscribe(
                    StrategySubscription(Quote, inst_id), strategy.strategy_id
                )
                self._router.subscribe(
                    StrategySubscription(Depth, inst_id), strategy.strategy_id
                )
        else:
            self._router.subscribe(StrategySubscription(Candle), strategy.strategy_id)
            self._router.subscribe(StrategySubscription(Quote), strategy.strategy_id)
            self._router.subscribe(StrategySubscription(Depth), strategy.strategy_id)

        self._disposables.setdefault(strategy.strategy_id, []).extend([
            self._bus.of_type(Candle).subscribe(self._wrap_on_bar(strategy)),
            self._bus.of_type(Quote).subscribe(self._wrap_on_quote(strategy)),
            self._bus.of_type(Depth).subscribe(self._wrap_on_depth(strategy)),
            self._bus.of_type(OrderFilled).subscribe(self._wrap_on_fill(strategy)),
        ])

    def unregister(self, strategy_id: str) -> None:
        """Unregister a strategy by ID, disposing only its subscriptions.

        Args:
            strategy_id: Unique identifier of the strategy to remove
        """
        self._strategies.pop(strategy_id, None)
        self._gated.discard(strategy_id)
        self._router.unsubscribe(strategy_id)
        self._dispose_strategy(strategy_id)

    def _dispose_strategy(self, strategy_id: str) -> None:
        """Dispose *only* this strategy's subscriptions (others untouched)."""
        for disposable in self._disposables.pop(strategy_id, []):
            try:
                disposable.dispose()
            except Exception:  # pragma: no cover
                pass

    def dispose_all(self) -> None:
        """Clean up all subscriptions and drop deferred orders."""
        for strategy_id in list(self._disposables):
            self._dispose_strategy(strategy_id)
            self._router.unsubscribe(strategy_id)
        self._disposables.clear()
        self._gated.clear()
        self._strategies.clear()
        self._pending.clear()

    # ------------------------------------------------------------------
    # Per-strategy lifecycle control (AC-8)
    # ------------------------------------------------------------------

    def pause_strategy(self, strategy_id: str) -> None:
        """Gate delivery to *strategy_id*, keeping its subscriptions.

        The strategy stops receiving events immediately (synchronously) but
        stays registered, so :meth:`resume_strategy` re-attaches without
        re-subscribing. Other strategies are untouched.
        """
        if strategy_id in self._strategies:
            self._gated.add(strategy_id)

    def resume_strategy(self, strategy_id: str) -> None:
        """Re-open delivery to *strategy_id* (undoes :meth:`pause_strategy`)."""
        self._gated.discard(strategy_id)

    def stop_strategy(
        self,
        strategy_id: str,
        *,
        cancel: Any | None = None,
        working_orders: Iterable[Any] = (),
    ) -> list[str]:
        """Stop *strategy_id* synchronously: gate, dispose, cancel working orders.

        - Delivery is gated immediately, on the call — not on the next event.
        - This strategy's subscriptions are disposed; no other strategy's are
          touched, so the rest of the book keeps running (AC-8 isolation).
        - Every *working* (non-terminal) order belonging to the strategy is
          passed to ``cancel(order_id)`` and the ids that could not be
          cancelled are returned. ``UNKNOWN`` is treated as working, mirroring
          the kill switch: an UNKNOWN order crossed the venue boundary and may
          be live, so it must be reached.
        - Open positions and their protective legs are deliberately NOT
          touched — flattening is a separate explicit command, not part of a
          stop. This method only ever *reads* ``working_orders``.

        ``cancel`` is a caller-supplied callable (the execution engine's own
        ``cancel``) so the strategy layer never imports the execution layer.
        """
        # 1. Synchronous delivery gate + drop this strategy's subscriptions.
        self._gated.add(strategy_id)
        self._dispose_strategy(strategy_id)
        # 2. Cancel working orders, UNKNOWN-aware. Positions are never read.
        failures: list[str] = []
        if cancel is not None:
            for order in working_orders:
                # Isolation is enforced HERE, not left to the caller's
                # filtering: an order belonging to another strategy is never
                # cancelled, even if the caller hands us a mixed list. An
                # order with no strategy_id is left alone too -- it is not
                # provably this strategy's, and "probably" is how one
                # strategy's stop flattens another's book.
                if getattr(order, "strategy_id", None) != strategy_id:
                    continue
                if order.status in _SETTLED_STATUSES:
                    continue
                try:
                    cancel(order.order_id)
                except Exception as exc:  # noqa: BLE001 — one failure must not stop the rest
                    log.error(
                        "stop_strategy %s: cancel failed for %s: %s",
                        strategy_id, order.order_id, exc,
                    )
                    failures.append(str(order.order_id.value))
        return failures

    @property
    def strategies(self) -> dict:
        """Return registered strategies."""
        return dict(self._strategies)

    def order_stats(self, strategy_id: str) -> dict[str, int]:
        """One strategy's order outcomes: failures and expired deferrals.

        F-47: live publishes ``ErrorOccurred`` and bumps one session-global
        counter, so a strategy whose every order is refused reads the same as
        one that is healthy. The backtest re-raises and aborts the run, so this
        asymmetry is the live-only half of the parity gap. Keyed by strategy id
        because "the counter went up" does not tell an operator which strategy
        to stop.
        """
        return {
            "order_failures": self._order_failures.get(strategy_id, 0),
            "orders_expired": self._orders_expired.get(strategy_id, 0),
        }


