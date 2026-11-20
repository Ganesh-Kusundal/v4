"""Contract: a replay session may never reach the venue."""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_config.schema import AppConfig
from tradex_domain import Candle, OHLC, Equity, Price, Quantity, Timeframe, OrderPlaced
from tradex_domain.events import OrderFilled
from tradex_reactive.bus import ReactiveBus
from tradex_runtime.startup import boot
from trading.tests.risk_support import permissive_risk

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def test_replay_session_never_reaches_venue() -> None:
    config = AppConfig(risk=permissive_risk(), mode="replay")
    session = boot(config)

    # Spy on ExecutionEngine.submit.
    submit_calls: list = []
    original_submit = session.engine.submit

    def _submit(request):
        submit_calls.append(request)
        return original_submit(request)

    session.engine.submit = _submit  # type: ignore[assignment]

    # Spy on the fill source (SimulatedFillSource exposes submit_calls already).
    fill_source = getattr(session.engine, "_fill", None)
    if fill_source is not None and hasattr(fill_source, "submit_calls"):
        baseline_fs = fill_source.submit_calls
    else:
        baseline_fs = 0

    # Spy on the broker's submit_order if present.
    broker_submit_calls: list = []
    broker = getattr(session, "broker", None)
    if broker is not None and hasattr(broker, "submit_order"):
        original_broker_submit = broker.submit_order

        def _broker_submit(*args, **kwargs):
            broker_submit_calls.append((args, kwargs))
            return original_broker_submit(*args, **kwargs)

        broker.submit_order = _broker_submit  # type: ignore[assignment]

    # Collect OrderPlaced events.
    placed: list[OrderPlaced] = []
    session.bus.of_type(OrderPlaced).subscribe(placed.append)

    try:
        # Feed a few bars/candles; replay must remain observation-only.
        bus = session.bus
        for i in range(5):
            price = Price(value=Decimal("100"))
            candle = Candle(
                instrument=INSTRUMENT,
                timeframe=Timeframe.M1,
                ohlc=OHLC(open=price, high=price, low=price, close=price),
                volume=Quantity(value=Decimal("1000")),
                timestamp=__import__("datetime").datetime(2026, 8, 5, 9, 15 + i, tzinfo=__import__("datetime").timezone.utc),
            )
            bus.publish(candle)

        # Assertions.
        assert submit_calls == [], "ExecutionEngine.submit was invoked in replay mode"
        assert placed == [], "OrderPlaced reached the bus in replay mode"
        assert broker_submit_calls == [], "broker.submit_order was invoked in replay mode"
        if fill_source is not None and hasattr(fill_source, "submit_calls"):
            assert fill_source.submit_calls == baseline_fs, (
                "fill source submit_calls increased in replay mode"
            )
    finally:
        session.stop()
