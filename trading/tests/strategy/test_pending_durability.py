"""Tests for ``ReactiveStrategyEngine._pending`` durability (G5).

Covers the principal-architect review of the deferred-order queue:

- ``_pending`` is indexed by ``InstrumentId`` so ``flush_pending`` runs in O(k)
  for the *k* orders pending on that instrument, not O(n) over all pending
  orders.
- A signal emitted on the last bar of a dataset is not silently dropped
  (the engine must still surface it through ``pending_snapshot`` /
  ``flush_pending``).
- Orders that sit in the queue longer than ``pending_max_age_bars`` are
  cancelled with an ``OrderCancelled`` event and a log warning, so a
  strategy that signals on a delisted / halted symbol cannot leave an
  order pending forever.
- ``dispose_all()`` empties the queue cleanly.

Ponytail:
- Public API (``pending_snapshot`` returns a tuple) is preserved.
- The internal ``_pending`` is private; only its semantics change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from tradex_domain import (
    OHLC,
    Candle,
    Equity,
    Price,
    Quantity,
)
from tradex_domain.enums import OrderSide
from tradex_domain.events import OrderCancelled, PlaceOrderCommand
from tradex_domain.strategy import Signal

from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.strategy.core.engine import ReactiveStrategyEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 8, 29, 10, 0, tzinfo=UTC)


def _ts(seconds: int) -> datetime:
    # Avoid second=60 (illegal in datetime); wrap to a minute bump.
    return datetime(2026, 8, 29, 10, 0, seconds % 60, tzinfo=UTC)


def _eq(symbol: str) -> Equity:
    return Equity.of("NSE", symbol)


def _candle(
    symbol: str,
    open_: float,
    close: float,
    ts: datetime,
) -> Candle:
    return Candle(
        instrument=_eq(symbol),
        timeframe=None,  # type: ignore[arg-type]
        ohlc=OHLC(
            open=Price(value=Decimal(str(open_))),
            high=Price(value=Decimal(str(max(open_, close) + 1))),
            low=Price(value=Decimal(str(min(open_, close) - 1))),
            close=Price(value=Decimal(str(close))),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
    )


@dataclass
class _DeferringStrategy:
    """Strategy that always emits a BUY signal of *strength*.

    When registered, every bar it sees yields a ``Signal`` for the bar's
    instrument. The engine's ``next_open`` fill reference defers those
    signals to the next candle's open — exactly the path that fills
    ``_pending`` and exercises the G5 changes.
    """

    strategy_id: str = "defer"
    strength: float = 1.0
    signals_emitted: list[Signal] = field(default_factory=list)

    def on_bar(self, ctx: Any, candle: Candle) -> Signal:
        sig = Signal(
            instrument=candle.instrument,
            direction=OrderSide.BUY,
            strength=self.strength,
            reason="g5-test",
        )
        self.signals_emitted.append(sig)
        return sig

    def on_quote(self, ctx: Any, quote: Any) -> None:
        return None

    def on_fill(self, ctx: Any, fill: Any) -> None:
        return None


def _capture_bus(bus: ReactiveBus) -> list[Any]:
    """Subscribe a sink that records every published message."""
    sink: list[Any] = []
    bus.subscribe(lambda msg: sink.append(msg))
    return sink


# ---------------------------------------------------------------------------
# 1) _pending is indexed by InstrumentId
# ---------------------------------------------------------------------------


def test_pending_is_indexed_by_instrument() -> None:
    """Flushing bar A only flushes A's deferred orders; B's stay queued."""
    bus = ReactiveBus()
    engine = ReactiveStrategyEngine(bus, fill_reference="next_open")
    strat = _DeferringStrategy("idx")
    engine.register(strat)

    captured = _capture_bus(bus)
    # Three bars of A and two bars of B interleaved. Each bar emits a signal
    # that is deferred to the next candle of the same instrument.
    a1 = _candle("AAA", 100.0, 101.0, _ts(0))
    a2 = _candle("AAA", 101.0, 102.0, _ts(60))
    a3 = _candle("AAA", 102.0, 103.0, _ts(120))
    b1 = _candle("BBB", 50.0, 51.0, _ts(30))
    b2 = _candle("BBB", 51.0, 52.0, _ts(90))

    # Interleave: A, B, A, B, A — but only A's next-A bars flush A's queue.
    bus.publish(a1)   # defer signal (queued under AAA)
    bus.publish(b1)   # defer signal (queued under BBB)
    bus.publish(a2)   # flushes AAA queue (1 fill), defers new signal
    bus.publish(b2)   # flushes BBB queue (1 fill), defers new signal
    bus.publish(a3)   # flushes AAA queue (1 fill), defers new signal

    commands = [m for m in captured if isinstance(m, PlaceOrderCommand)]
    # A produced 2 fills (a2, a3), B produced 1 fill (b2). a1's signal was
    # deferred past a1 (no A bar exists at the start) and flushed at a2.
    assert len(commands) == 3, f"unexpected publish count: {len(commands)}"
    instruments = [c.request.instrument.symbol for c in commands]
    # First a fill is from the a1-deferred signal (filled at a2.open=101);
    # then b1-deferred signal (filled at b2.open=51); then a2-deferred
    # signal (filled at a3.open=102). The two a-fills are clearly at AAA.
    assert instruments.count("AAA") == 2
    assert instruments.count("BBB") == 1

    engine.dispose_all()


# ---------------------------------------------------------------------------
# 2) Last-bar flushes pending orders
# ---------------------------------------------------------------------------


def test_last_bar_flushes_pending_orders() -> None:
    """A signal on the final bar is not silently dropped.

    Mirrors the principal-architect scenario: a single-bar dataset ends
    with the strategy still holding deferred orders. The engine must
    surface them through ``pending_snapshot`` so the caller (replay /
    backtest) can drive the last-bar flush itself.
    """
    bus = ReactiveBus()
    engine = ReactiveStrategyEngine(bus, fill_reference="next_open")
    strat = _DeferringStrategy("last")
    engine.register(strat)

    captured = _capture_bus(bus)
    only = _candle("ONLY", 100.0, 101.0, _ts(0))
    bus.publish(only)

    # Engine deferred the signal (no PlaceOrderCommand yet on the bus).
    commands_before = [m for m in captured if isinstance(m, PlaceOrderCommand)]
    assert len(commands_before) == 0
    # But pending_snapshot exposes the deferred order — the backtest driver
    # uses this to drive the last-bar flush (see replay/backtest.py).
    pending = engine.pending_snapshot()
    assert len(pending) == 1
    assert pending[0]["instrument_id"].underlying == "ONLY"

    # The driver flushes at the last bar — the engine must publish.
    engine.flush_pending(only)
    commands_after = [m for m in captured if isinstance(m, PlaceOrderCommand)]
    assert len(commands_after) == 1
    assert commands_after[0].request.instrument.symbol == "ONLY"
    # Open is 100.0 — fill reference is "next_open" so the fill is at
    # the same bar's open (the only bar) at 100.
    assert commands_after[0].request.price.value == Decimal("100")

    engine.dispose_all()


# ---------------------------------------------------------------------------
# 3) pending_max_age_bars cancels stale orders
# ---------------------------------------------------------------------------


def test_pending_max_age_cancels_stale_orders() -> None:
    """Orders older than ``pending_max_age_bars`` are cancelled.

    With ``pending_max_age_bars=2`` and an instrument that never gets
    another bar (delisted / halted), a deferred signal aged 3 bars must
    be cancelled and ``OrderCancelled`` published.
    """
    bus = ReactiveBus()
    engine = ReactiveStrategyEngine(
        bus, fill_reference="next_open", pending_max_age_bars=2,
    )
    strat = _DeferringStrategy("stale")
    engine.register(strat)

    captured = _capture_bus(bus)
    target = _candle("STALE", 100.0, 101.0, _ts(0))
    other1 = _candle("OTHER", 200.0, 201.0, _ts(10))
    other2 = _candle("OTHER", 201.0, 202.0, _ts(20))
    other3 = _candle("OTHER", 202.0, 203.0, _ts(30))

    bus.publish(target)    # defer a signal on STALE
    bus.publish(other1)    # bar 1 — STALE age = 1, no expiry yet
    bus.publish(other2)    # bar 2 — STALE age = 2, no expiry yet
    bus.publish(other3)    # bar 3 — STALE age = 3, cancel (>= max_age)

    cancels = [m for m in captured if isinstance(m, OrderCancelled)]
    assert len(cancels) == 1, f"expected one cancel, got {len(cancels)}"
    cancelled_order = cancels[0].order
    assert cancelled_order.instrument.symbol == "STALE"
    assert cancelled_order.status.value == "CANCELLED"

    # And the STALE queue is empty (OTHER may have new pending signals
    # from the strategy's per-bar signal, which is fine).
    remaining_symbols = {
        p["instrument_id"].underlying for p in engine.pending_snapshot()
    }
    assert "STALE" not in remaining_symbols

    engine.dispose_all()


def test_pending_max_age_warns(caplog: Any) -> None:
    """The engine logs a warning when it cancels a stale pending order."""
    bus = ReactiveBus()
    engine = ReactiveStrategyEngine(
        bus, fill_reference="next_open", pending_max_age_bars=1,
    )
    strat = _DeferringStrategy("warn")
    engine.register(strat)

    # Defer on DEFER, then advance with OTHER bars (different instrument so
    # the deferred signal isn't flushed by accident).
    defer = _candle("DEFER", 10.0, 11.0, _ts(0))
    other1 = _candle("OTHER", 100.0, 101.0, _ts(10))
    other2 = _candle("OTHER", 101.0, 102.0, _ts(20))

    caplog.set_level(logging.WARNING, logger="tradex_trading.strategy.core.engine")
    bus.publish(defer)   # defer (bar 1)
    bus.publish(other1)  # bar 2 — age=1, no cancel yet
    bus.publish(other2)  # bar 3 — age=2 > 1, cancel

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("cancelled" in r.getMessage().lower() for r in warnings), (
        f"no warning emitted: {[r.getMessage() for r in warnings]}"
    )
    engine.dispose_all()


# ---------------------------------------------------------------------------
# 4) dispose_all does not leave pending orders
# ---------------------------------------------------------------------------


def test_dispose_all_does_not_leave_pending() -> None:
    """dispose_all() empties the pending queue (dict, not just list)."""
    bus = ReactiveBus()
    engine = ReactiveStrategyEngine(bus, fill_reference="next_open")
    strat = _DeferringStrategy("disc")
    engine.register(strat)

    bus.publish(_candle("DISC", 1.0, 2.0, _ts(0)))
    bus.publish(_candle("DISC", 2.0, 3.0, _ts(10)))

    assert len(engine.pending_snapshot()) >= 1
    engine.dispose_all()
    # The pending dict is empty — pending_snapshot returns an empty tuple.
    assert engine.pending_snapshot() == ()
    # And the internal container itself is empty (a dict, not a list).
    assert len(engine._pending) == 0


# ---------------------------------------------------------------------------
# 5) Backward-compat: default pending_max_age_bars = 0 (no expiry)
# ---------------------------------------------------------------------------


def test_default_pending_max_age_keeps_orders_forever() -> None:
    """With pending_max_age_bars=0 (default), no order is ever cancelled."""
    bus = ReactiveBus()
    engine = ReactiveStrategyEngine(bus, fill_reference="next_open")
    strat = _DeferringStrategy("keep")
    engine.register(strat)

    captured = _capture_bus(bus)
    bus.publish(_candle("KEEP", 1.0, 2.0, _ts(0)))
    for i in range(5):
        bus.publish(_candle("KEEP", float(2 + i), float(3 + i), _ts(10 + i)))

    cancels = [m for m in captured if isinstance(m, OrderCancelled)]
    assert cancels == []
    engine.dispose_all()
