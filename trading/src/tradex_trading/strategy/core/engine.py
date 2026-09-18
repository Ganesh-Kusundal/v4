"""Reactive strategy engine — strategies subscribe to typed Observable streams."""

from __future__ import annotations

import hashlib
import logging
from decimal import Decimal
from typing import Any

from tradex_domain.enums import OrderStatus, OrderType, TimeInForce
from tradex_domain.events import OrderCancelled, PlaceOrderCommand
from tradex_domain.execution import Order
from tradex_domain.instruments import InstrumentId
from tradex_domain.strategy import Signal, StrategyContext
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity

from tradex_trading.strategy.core.brackets import protective_request

log = logging.getLogger(__name__)

_FILL_REFERENCES = frozenset({"next_open", "signal_close"})


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
    """

    def __init__(
        self,
        bus,
        *,
        fill_reference: str = "next_open",
        pending_max_age_bars: int = 0,
    ) -> None:
        """Initialize with a ReactiveBus.

        Args:
            bus: ReactiveBus instance for event streaming
            fill_reference: ``"next_open"`` (default) or ``"signal_close"`` —
                see the class docstring.
            pending_max_age_bars: maximum number of bars a deferred
                ``next_open`` order may sit in the queue before the engine
                cancels it with ``OrderCancelled`` (parity review G5). ``0``
                disables expiry (backward-compatible). Tracking counts the
                number of bars processed since the order was deferred, so a
                value of ``N`` cancels the order on the ``N+1``-th bar that
                does not match its instrument.
        """
        if fill_reference not in _FILL_REFERENCES:
            raise ValueError(f"unknown fill_reference: {fill_reference!r}")
        if pending_max_age_bars < 0:
            raise ValueError(
                f"pending_max_age_bars must be >= 0, got {pending_max_age_bars!r}"
            )
        self._fill_reference = fill_reference
        self._bus = bus
        self._strategies: dict[str, Any] = {}
        self._disposables: list = []
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

    def _make_context(self, **overrides: Any) -> StrategyContext:
        """Create a StrategyContext with current engine state."""
        return StrategyContext(**overrides)

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
            ctx = self._make_context(
                bar_count=self._bar_count,
                timestamp=getattr(candle, "timestamp", None),
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
                # Declared protective levels ride the deferred signal onto the
                # order, validated against the open this order actually fills
                # at — not the close the strategy saw when it declared them.
                request = protective_request(
                    signal=pending["signal"],
                    entry=open_price,
                    quantity=pending["quantity"],
                    correlation_id=pending["correlation_id"],
                    tag=f"{pending['strategy_id']}@{version}",
                    reference_timestamp=getattr(candle, "timestamp", None),
                )
                self._bus.publish(PlaceOrderCommand(request=request))
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
        self._bus.publish(OrderCancelled(order=order))

    def _wrap_on_quote(self, strategy: Any) -> Any:
        """Wrap strategy.on_quote to inject context and bridge Signal→Order."""
        def handler(quote: Any) -> Any:
            ctx = self._make_context(timestamp=getattr(quote, "timestamp", None))
            result = strategy.on_quote(ctx, quote)
            self._maybe_publish_order(result, strategy, quote)
            return result
        return handler

    def _wrap_on_depth(self, strategy: Any) -> Any:
        """Wrap strategy.on_depth to inject context."""
        def handler(depth: Any) -> Any:
            ctx = self._make_context(timestamp=getattr(depth, "timestamp", None))
            if hasattr(strategy, "on_depth"):
                result = strategy.on_depth(ctx, depth)
                self._maybe_publish_order(result, strategy, depth)
                return result
        return handler

    def _wrap_on_fill(self, strategy: Any) -> Any:
        """Wrap strategy.on_fill to inject context."""
        def handler(fill: Any) -> Any:
            ctx = self._make_context()
            return strategy.on_fill(ctx, fill)
        return handler

    def _maybe_publish_order(
        self, result: Any, strategy: Any, event: Any = None,
    ) -> None:
        """If *result* is a Signal, publish a PlaceOrderCommand to the bus.

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
        qty_value = abs(result.strength) if result.strength else 1.0
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
            inst_id = result.instrument.instrument_id
            self._pending.setdefault(inst_id, []).append({
                "strategy_id": strategy_id,
                "strategy_version": version,
                "instrument_id": inst_id,
                "signal": result,
                "quantity": Quantity(Decimal(str(qty_value))),
                "correlation_id": correlation_id,
                "deferred_at_bar": self._bar_count,
            })
            return
        price = self._reference_price(event)
        if price is None:
            raise ValueError(
                f"strategy {strategy_id} emitted {result.reason!r} without a "
                "priced event (candle/quote/depth) to reference"
            )
        request = protective_request(
            signal=result,
            entry=price,
            quantity=Quantity(Decimal(str(qty_value))),
            correlation_id=correlation_id,
            tag=tag,
            reference_timestamp=getattr(event, "timestamp", None),
        )
        self._bus.publish(PlaceOrderCommand(request=request))

    def register(self, strategy) -> None:
        """Register a strategy and subscribe it to market events.

        Args:
            strategy: Strategy instance implementing the Strategy protocol
        """
        self._strategies[strategy.strategy_id] = strategy

        from tradex_domain import Candle, Fill, Quote
        from tradex_domain.market import Depth

        self._disposables.extend([
            self._bus.of_type(Candle).subscribe(self._wrap_on_bar(strategy)),
            self._bus.of_type(Quote).subscribe(self._wrap_on_quote(strategy)),
            self._bus.of_type(Depth).subscribe(self._wrap_on_depth(strategy)),
            self._bus.of_type(Fill).subscribe(self._wrap_on_fill(strategy)),
        ])

    def unregister(self, strategy_id: str) -> None:
        """Unregister a strategy by ID.

        Args:
            strategy_id: Unique identifier of the strategy to remove
        """
        self._strategies.pop(strategy_id, None)

    def dispose_all(self) -> None:
        """Clean up all subscriptions and drop deferred orders."""
        for disposable in self._disposables:
            try:
                disposable.dispose()
            except Exception:  # pragma: no cover
                pass
        self._disposables.clear()
        self._strategies.clear()
        self._pending.clear()

    @property
    def strategies(self) -> dict:
        """Return registered strategies."""
        return dict(self._strategies)


__all__ = ["ReactiveStrategyEngine"]
