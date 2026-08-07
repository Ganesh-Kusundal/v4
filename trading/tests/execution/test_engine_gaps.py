"""Gap-coverage tests for ExecutionEngine, RiskManager, kill switch,
sync-submit boundary/idempotency, and AnalyticsEngine warmup_bars.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType
from tradex_domain.errors import OrderRejectedError, OrderSubmissionUnknownError
from tradex_domain.execution import Order, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.market import OHLC, Candle, HistoricalSeries, Timeframe
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity

from tradex_trading.execution.engine import (
    ExecutionEngine,
    MemoryIdempotencyGuard,
    RiskCheckResult,
    RiskManager,
)
from tradex_trading.execution.fill_sources import SimulatedFillSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _bus() -> MagicMock:
    bus = MagicMock()
    bus.of_type = MagicMock(return_value=MagicMock())
    bus.of_type.return_value.pipe = MagicMock(return_value=MagicMock())
    bus.of_type.return_value.pipe.return_value.subscribe = MagicMock()
    return bus


def _request(**overrides) -> OrderRequest:
    defaults = dict(
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
    )
    defaults.update(overrides)
    return OrderRequest(**defaults)


def _make_engine(**kwargs) -> ExecutionEngine:
    kw: dict = {"bus": _bus(), "fill_source": SimulatedFillSource()}
    kw.update(kwargs)
    return ExecutionEngine(**kw)


def _make_order(
    order_id: str = "o-1",
    status: OrderStatus = OrderStatus.NEW,
) -> Order:
    return Order(
        order_id=OrderId(value=order_id),
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force="DAY",
        status=status,
    )


# ===================================================================
# ExecutionEngine cancel / get_order / all_orders
# ===================================================================


def test_cancel_transitions_to_cancelled() -> None:
    engine = _make_engine()
    order = _make_order("cancel-1", OrderStatus.NEW)
    engine.cache.update_order(order)

    result = engine.cancel(OrderId(value="cancel-1"))
    assert result.status is OrderStatus.CANCELLED


def test_cancel_raises_for_unknown() -> None:
    engine = _make_engine()
    with pytest.raises(OrderRejectedError):
        engine.cancel(OrderId(value="nonexistent"))


def test_get_order_returns_existing() -> None:
    engine = _make_engine()
    order = _make_order("get-1")
    engine.cache.update_order(order)

    found = engine.get_order(OrderId(value="get-1"))
    assert found is not None
    assert found.order_id.value == "get-1"


def test_all_orders_after_submits() -> None:
    engine = _make_engine()
    engine._process_request(_request())
    engine._process_request(_request())
    assert len(engine.all_orders()) == 2


# ===================================================================
# RiskManager check_order
# ===================================================================


def test_check_order_approved() -> None:
    rm = RiskManager()
    result = rm.check_order(_request())
    assert result == RiskCheckResult(approved=True, reason="")


def test_check_order_rejected() -> None:
    rm = RiskManager(live_orders_enabled=False)
    result = rm.check_order(_request())
    assert result.approved is False
    assert result.reason == "risk_check_failed"


# ===================================================================
# Kill switch
# ===================================================================


def test_trip_kill_returns_empty_on_success() -> None:
    engine = _make_engine()
    # Submit an order — it will be FILLED (terminal status)
    engine._process_request(_request())
    failures = engine.trip_kill_switch(reason="test")
    assert failures == []


def test_kill_switch_propagates_to_risk_manager() -> None:
    rm = RiskManager()
    engine = _make_engine(risk_manager=rm)
    assert rm.live_orders_enabled is True

    engine.trip_kill_switch(reason="halt")
    assert rm.live_orders_enabled is False


# ===================================================================
# Sync submit — boundary crossed / idempotency release
# ===================================================================


def test_sync_submit_boundary_crossed_raises() -> None:
    fill_source = MagicMock()
    fill_source.submission_boundary_crossed = True
    fill_source.submit.side_effect = OSError("connection lost")

    engine = _make_engine(fill_source=fill_source)
    with pytest.raises(OrderSubmissionUnknownError):
        engine.submit(_request())


def test_sync_submit_releases_key_on_non_boundary_error() -> None:
    fill_source = MagicMock()
    fill_source.submission_boundary_crossed = False  # explicit: not a boundary error
    fill_source.submit.side_effect = OSError("fill error")

    guard = MemoryIdempotencyGuard()
    engine = _make_engine(fill_source=fill_source, idempotency_guard=guard)

    cid = CorrelationId(value="release-test")
    req = _request(correlation_id=cid)

    # First submit fails (non-boundary) → key should be released
    engine.submit(req)

    # Key was released, so we can reserve it again
    dup = guard.check_and_reserve(cid)
    assert dup is None  # None means reservation succeeded (not a duplicate)


# ===================================================================
# AnalyticsEngine warmup_bars
# ===================================================================


def _make_series(n: int = 30) -> HistoricalSeries:
    inst = _eq()
    candles: list[Candle] = []
    for i in range(n):
        candles.append(
            Candle(
                instrument=inst,
                timeframe=Timeframe("1d"),
                ohlc=OHLC(
                    open=Price(value=Decimal(str(100 + i))),
                    high=Price(value=Decimal(str(105 + i))),
                    low=Price(value=Decimal(str(95 + i))),
                    close=Price(value=Decimal(str(102 + i))),
                ),
                volume=Decimal("1000"),
                timestamp=datetime(2024, 1, i + 1, tzinfo=UTC),
            ),
        )
    return HistoricalSeries(
        instrument=inst,
        timeframe=Timeframe("1d"),
        candles=candles,
        start=candles[0].timestamp,
        end=candles[-1].timestamp,
    )


def test_warmup_bars_pads_with_none() -> None:
    from tradex_trading.analytics.engine import AnalyticsEngine

    series = _make_series(30)
    engine = AnalyticsEngine(warmup_bars=5)
    result = engine.indicator(series, "sma", period=10)

    # sma(period=10) → 9 Nones + 21 values; padded = 5 Nones + 21 values = 26
    # Result has 26 candles; first 5 retain original close values (None-padded)
    # Original candles[4..8] have closes 106..110
    assert len(result.candles) == 26
    for i in range(5):
        expected_close = Decimal(str(102 + 4 + i))  # candles[4+i].close
        assert result.candles[i].ohlc.close.value == expected_close


def test_warmup_bars_zero_no_padding() -> None:
    from tradex_trading.analytics.engine import AnalyticsEngine

    series = _make_series(30)
    engine = AnalyticsEngine(warmup_bars=0)
    result = engine.indicator(series, "sma", period=3)

    # sma(period=3) on 30 values → 28 non-None values
    # Result replaces trailing 28 candles
    assert len(result.candles) == 28
