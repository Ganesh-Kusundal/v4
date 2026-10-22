"""A run's correlation ids must be reproducible inside the run and unique
across runs.

The id used to be seeded by ``strategy_id:event_type:timestamp:seq`` only, so
two processes replaying the same tape emitted the same id for the same signal.
The durable idempotency guard keys on that id: the second process replayed the
first process's receipt and never reached the venue.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import Candle, OHLC, OrderRequest, OrderSide, Signal
from tradex_domain.enums import ExchangeId, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_reactive.bus import ReactiveBus
from tradex_strategy.core.engine import ReactiveStrategyEngine
from tradex_strategy.core.sizing import PositionSizer

INSTRUMENT = Equity.of(ExchangeId.NSE, "RELIANCE")


class _Buyer:
    strategy_id = "corr_test"
    version = "1.0.0"

    def on_start(self, context) -> None:
        """No-op."""

    def on_stop(self, context) -> None:
        """No-op."""

    def on_bar(self, context, bar) -> Signal:
        return Signal(
            instrument=INSTRUMENT,
            direction=OrderSide.BUY,
            strength=1.0,
            reason="corr_scope",
        )

    def on_quote(self, context, quote):
        return None

    def on_fill(self, context, fill) -> None:
        """No-op."""

    def on_event(self, event) -> None:
        """No-op."""


def _candle(minute: int) -> Candle:
    price = Price(value=Decimal("100"))
    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.M1,
        ohlc=OHLC(open=price, high=price, low=price, close=price),
        volume=Quantity(value=Decimal("10")),
        timestamp=datetime(2026, 8, 7, 9, minute, tzinfo=UTC),
    )


def _sizer() -> PositionSizer:
    return PositionSizer(
        max_order_value=Decimal("10000"),
        fallback_notional=Decimal("10000"),
    )


def _submitted(run_id: str | None, minute: int) -> list[OrderRequest]:
    bus = ReactiveBus()
    requests: list[OrderRequest] = []
    engine = ReactiveStrategyEngine(
        bus,
        requests.append,
        fill_reference="signal_close",
        sizer=_sizer(),
        run_id=run_id,
    )
    engine.register(_Buyer())
    try:
        bus.publish(_candle(minute))
    finally:
        engine.dispose_all()
    return requests


def test_same_run_id_reproduces_the_same_correlation_id() -> None:
    first = _submitted("run-a", 0)
    second = _submitted("run-a", 0)
    assert len(first) == len(second) == 1
    assert first[0].correlation_id == second[0].correlation_id


def test_different_run_ids_never_collide() -> None:
    first = _submitted("run-a", 0)
    second = _submitted("run-b", 0)
    assert len(first) == len(second) == 1
    assert first[0].correlation_id != second[0].correlation_id


def test_default_runs_do_not_collide() -> None:
    first = _submitted(None, 0)
    second = _submitted(None, 0)
    assert len(first) == len(second) == 1
    assert first[0].correlation_id != second[0].correlation_id


def test_distinct_events_in_one_run_still_differ() -> None:
    bus = ReactiveBus()
    requests: list[OrderRequest] = []
    engine = ReactiveStrategyEngine(
        bus,
        requests.append,
        fill_reference="signal_close",
        sizer=_sizer(),
        run_id="run-a",
    )
    engine.register(_Buyer())
    try:
        bus.publish(_candle(0))
        bus.publish(_candle(1))
    finally:
        engine.dispose_all()
    assert len(requests) == 2
    assert requests[0].correlation_id != requests[1].correlation_id
