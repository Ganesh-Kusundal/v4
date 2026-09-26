"""Gap tests for execution engine — risk manager, idempotency, reconcile."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.events import OrderFilled
from tradex_domain.execution import Fill, Order, OrderRequest, Position
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import (
    CorrelationId,
    Money,
    OrderId,
    Price,
    Quantity,
)

from tradex_trading.execution.engine import ExecutionEngine, RiskManager
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.execution.idempotency import MemoryIdempotencyGuard
from tradex_trading.reactive.bus import ReactiveBus


def _make_request(
    symbol: str = "TEST",
    side: OrderSide = OrderSide.BUY,
    quantity: str = "10",
    price: str = "100",
) -> OrderRequest:
    instrument = Equity.of("NSE", symbol)
    return OrderRequest(
        instrument=instrument,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(quantity)),
        price=Price(value=Decimal(price)),
        time_in_force=TimeInForce.DAY,
    )


def _make_order(
    order_id: str = "oid-1",
    symbol: str = "TEST",
    status: OrderStatus = OrderStatus.NEW,
) -> Order:
    instrument = Equity.of("NSE", symbol)
    return Order(
        order_id=OrderId(value=order_id),
        instrument=instrument,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=status,
    )


def _make_position(symbol: str, qty: str) -> Position:
    instrument = Equity.of("NSE", symbol)
    return Position(
        instrument=instrument,
        quantity=Quantity(value=Decimal(qty)),
        avg_price=Price(value=Decimal("100")),
        realized_pnl=Money(amount=Decimal("0")),
        unrealized_pnl=Money(amount=Decimal("0")),
    )


def _make_engine(**kwargs) -> ExecutionEngine:
    bus = ReactiveBus()
    fill = SimulatedFillSource()
    return ExecutionEngine(bus=bus, fill_source=fill, **kwargs)


def _market_request(
    price: str | None = None,
    quantity: str = "10",
) -> OrderRequest:
    """MARKET order — optionally without a price (the bypass case)."""
    instrument = Equity.of("NSE", "TEST")
    return OrderRequest(
        instrument=instrument,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal(quantity)),
        price=Price(value=Decimal(price)) if price is not None else None,
        time_in_force=TimeInForce.DAY,
    )


def test_risk_manager_market_order_notional_uses_mark() -> None:
    """A MARKET order without a price is notional-marked via price_provider,
    so the order-value gate is not silently bypassed (real-money correctness)."""
    rm = RiskManager(
        max_order_value=Decimal("500"),
        price_provider=lambda inst: Price(value=Decimal("100")),
    )
    req = _market_request(price=None, quantity="10")  # marked 100 * 10 = 1000
    assert rm._incoming_exposure(req) == Decimal("1000")
    # 1000 > 500 -> value gate trips even though request.price is None.
    assert rm.check(req) is False


def test_risk_manager_market_order_with_price_still_gated() -> None:
    """MARKET order carrying a price uses that price (unchanged behavior)."""
    rm = RiskManager(max_order_value=Decimal("500"))
    assert rm.check(_market_request(price="100", quantity="10")) is False
    assert rm.check(_market_request(price="10", quantity="10")) is True


def test_risk_manager_market_order_unknown_mark_flag() -> None:
    """When no mark can be resolved, reject_unknown_market_value decides:
    True -> deny (fail-closed live); False -> preserve fallback (dev)."""
    rm_closed = RiskManager(
        max_order_value=Decimal("500"),
        reject_unknown_market_value=True,
    )
    assert rm_closed.check(_market_request(price=None)) is False

    rm_open = RiskManager(
        max_order_value=Decimal("500"),
        reject_unknown_market_value=False,
    )
    assert rm_open.check(_market_request(price=None)) is True


def _make_pnl_position(symbol: str, qty: str, unrealized: str) -> Position:
    """Position with a given unrealized PnL (for daily-loss / drawdown guards)."""
    pos = _make_position(symbol, qty)
    return Position(
        instrument=pos.instrument,
        quantity=pos.quantity,
        avg_price=pos.avg_price,
        realized_pnl=pos.realized_pnl,
        unrealized_pnl=Money(amount=Decimal(unrealized)),
    )


def test_risk_manager_daily_loss_denies_open_allows_reduce() -> None:
    """max_daily_loss_amt denies new exposure once breached, but always allows
    flattening/reduction so risk is never locked on a position."""
    holder = [Decimal("0"), Decimal("100")]  # [unrealized, qty]

    def positions():
        return [_make_pnl_position("TEST", str(holder[1]), str(holder[0]))]

    rm = RiskManager(max_daily_loss_amt=Decimal("500"), positions_provider=positions)
    buy = _make_request(price="100", quantity="10")  # increases long
    sell = OrderRequest(
        instrument=Equity.of("NSE", "TEST"),
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=Quantity(Decimal("5")),
        price=Price(Decimal("100")),
        time_in_force=TimeInForce.DAY,
    )

    holder[0] = Decimal("0")   # baseline day start
    assert rm.check(buy) is True
    holder[0] = Decimal("-600")  # breached -500
    assert rm.check(buy) is False   # new open denied
    assert rm.check(sell) is True   # reduction always allowed
    holder[0] = Decimal("-100")     # within limit
    assert rm.check(buy) is True


def test_risk_manager_drawdown_denies_open_allows_reduce() -> None:
    """max_drawdown_pct stops new exposure after a peak-to-trough drawdown."""
    holder = [Decimal("0"), Decimal("100")]

    def positions():
        return [_make_pnl_position("TEST", holder[1], holder[0])]

    rm = RiskManager(max_drawdown_pct=Decimal("0.5"), positions_provider=positions)
    buy = _make_request(price="100", quantity="10")
    sell = OrderRequest(
        instrument=Equity.of("NSE", "TEST"),
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=Quantity(Decimal("5")),
        price=Price(Decimal("100")),
        time_in_force=TimeInForce.DAY,
    )
    holder[0] = Decimal("0")       # baseline
    assert rm.check(buy) is True
    holder[0] = Decimal("1000")    # peak equity
    assert rm.check(buy) is True
    holder[0] = Decimal("300")     # 70% drawdown from peak 1000
    assert rm.check(buy) is False  # new open denied
    assert rm.check(sell) is True  # reduction allowed


def test_risk_manager_market_open_counts_against_position_value() -> None:
    """_position_exposure marks via provider (existing seam) — add a guard so a
    MARKET open is counted against max_position_value, not treated as zero."""
    rm = RiskManager(
        max_position_value=Decimal("1500"),
        price_provider=lambda _inst: Price(value=Decimal("100")),
    )
    # existing position
    rm.set_positions_provider(lambda: [_make_position("TEST", qty="10")])
    # 10 * 100 (existing) + 10 * 100 (incoming MARKET, no price) = 2000 > 1500
    assert rm.check(_market_request(price=None, quantity="10")) is False


# ---------------------------------------------------------------------------
# RiskManager tests
# ---------------------------------------------------------------------------


def test_risk_manager_rate_limit_rejects_after_cap() -> None:
    """RiskManager(max_orders_per_minute=3) rejects the 4th call."""
    rm = RiskManager(max_orders_per_minute=3)
    req = _make_request()
    assert rm.check(req) is True
    assert rm.check(req) is True
    assert rm.check(req) is True
    # 4th call should be rejected (rate limit reached)
    assert rm.check(req) is False


def test_risk_manager_max_position_value_rejects() -> None:
    """RiskManager rejects when exposure + incoming order exceed the cap."""
    rm = RiskManager(max_position_value=Decimal("50000"))
    req = _make_request(quantity="10", price="100")  # 1,000 notional
    # No positions provider -> no position check; order value itself is small.
    assert rm.check(req) is True

    # With a provider showing existing exposure, the cap is enforced.
    rm2 = RiskManager(
        max_position_value=Decimal("50000"),
        positions_provider=lambda: [_make_position("AAPL", qty="500")],
    )
    assert rm2.check(req) is False  # 500*100 + 10*100 = 51,000 > 50,000

    # Exposure exactly at the cap is allowed (only > rejects).
    rm3 = RiskManager(
        max_position_value=Decimal("50000"),
        positions_provider=lambda: [_make_position("AAPL", qty="490")],
    )
    assert rm3.check(req) is True  # 49,000 + 1,000 = 50,000 == cap

    # Small existing exposure passes.
    rm4 = RiskManager(
        max_position_value=Decimal("50000"),
        positions_provider=lambda: [_make_position("AAPL", qty="100")],
    )
    assert rm4.check(req) is True  # 10,000 + 1,000 = 11,000 <= 50,000


def test_engine_enforces_max_position_value_on_fills() -> None:
    """ExecutionEngine + cache-backed risk manager: a fill that pushes total
    position value over the cap is rejected end-to-end."""
    bus = ReactiveBus()
    engine = ExecutionEngine(bus=bus, fill_source=SimulatedFillSource())
    risk = RiskManager(max_position_value=Decimal("2000"))
    risk._positions_provider = engine.cache.all_positions  # type: ignore[attr-defined]
    engine._risk = risk
    try:
        receipt = engine.submit(_make_request(quantity="10", price="100"))
        assert receipt.status == OrderStatus.FILLED  # 1,000
        receipt2 = engine.submit(_make_request(quantity="10", price="100"))
        assert receipt2.status == OrderStatus.FILLED  # 2,000 (== cap, allowed)
        # A third order pushes cumulative exposure to 3,000 > 2,000 -> reject.
        receipt3 = engine.submit(_make_request(quantity="10", price="100"))
        assert receipt3.status == OrderStatus.REJECTED
        assert "risk_check_failed" in receipt3.message
    finally:
        engine.shutdown()


# ---------------------------------------------------------------------------
# IdempotencyGuard tests
# ---------------------------------------------------------------------------


def test_idempotency_guard_double_reserve_raises() -> None:
    """Reserving the same correlation id twice raises RuntimeError."""
    guard = MemoryIdempotencyGuard()
    cid = CorrelationId(value="dup-key")
    first = guard.check_and_reserve(cid)
    assert first is None  # first reserve succeeds
    try:
        guard.check_and_reserve(cid)
    except RuntimeError as exc:
        assert "already reserved" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError on double reserve")


# ---------------------------------------------------------------------------
# ExecutionEngine — kill switch
# ---------------------------------------------------------------------------


def test_sync_submit_rejects_when_kill_switch_active() -> None:
    """submit() returns rejected receipt when kill switch is tripped."""
    engine = _make_engine()
    engine.trip_kill_switch(reason="test")
    req = _make_request()
    receipt = engine.submit(req)
    assert receipt.status == OrderStatus.REJECTED
    assert "kill_switch" in receipt.message


# ---------------------------------------------------------------------------
# Reconciliation tests
# ---------------------------------------------------------------------------


def test_reconcile_broker_orders_missing_local() -> None:
    """Reconcile detects broker orders not present in local cache."""
    engine = _make_engine()
    broker_order = _make_order(order_id="broker-only", status=OrderStatus.FILLED)
    drifts = engine.reconcile(broker_orders=[broker_order])
    assert len(drifts) >= 1
    assert any(d.kind == "order" and d.key == "broker-only" for d in drifts)


def test_reconcile_broker_orders_missing_remote() -> None:
    """Reconcile detects local orders not present at broker."""
    engine = _make_engine()
    local_order = _make_order(order_id="local-only", status=OrderStatus.NEW)
    engine.cache.update_order(local_order)
    drifts = engine.reconcile(broker_orders=[])
    assert len(drifts) >= 1
    assert any(d.reason == "missing broker order" for d in drifts)


def test_reconcile_combined_positions_and_orders() -> None:
    """Both positions and orders produce drift items."""
    engine = _make_engine()
    # Add a local position
    local_pos = _make_position("AAPL", qty="50")
    engine.cache.update_position(local_pos)
    # Broker has a different position for same symbol
    broker_pos = _make_position("AAPL", qty="30")
    # Broker has an order not in local cache
    broker_order = _make_order(order_id="extra-order", status=OrderStatus.FILLED)
    drifts = engine.reconcile(
        broker_positions=[broker_pos],
        broker_orders=[broker_order],
    )
    # Should have at least one position drift and one order drift
    pos_drifts = [d for d in drifts if d.kind == "position" and d.symbol]
    order_drifts = [d for d in drifts if d.kind == "order"]
    assert len(pos_drifts) >= 1
    assert len(order_drifts) >= 1


def test_risk_manager_rejected_count_increments() -> None:
    """rejected_count tracks every deny so backtest can report num_rejected."""
    rm = RiskManager(max_order_value=Decimal("1"))
    req = OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("100")),
    )
    assert rm.check(req) is False
    assert rm.check(req) is False
    assert rm.rejected_count == 2
    # A passing check does not change the count.
    rm2 = RiskManager()
    assert rm2.check(req) is True
    assert rm2.rejected_count == 0


# ---------------------------------------------------------------------------
# fill dedup bounding (C1)
# ---------------------------------------------------------------------------


def test_applied_fills_bounded_to_prevent_memory_leak() -> None:
    """FillDedup LRU is bounded at the configured max to cap memory growth."""
    from tradex_trading.execution.idempotency import FillDedup

    engine = _make_engine()
    # Lower the cap for testing by replacing the FillDedup.
    engine._fill_dedup = FillDedup(max_size=100)

    instrument = Equity.of("NSE", "TEST")
    for i in range(150):
        fill = Fill(
            order_id=OrderId(value=f"ord-{i}"),
            instrument=instrument,
            side=OrderSide.BUY,
            quantity=Quantity(Decimal("10")),
            price=Price(Decimal("100")),
            fill_id=f"fill-{i}",
        )
        engine._apply_fill(OrderFilled(fill=fill))

    # After 150 unique fills with max=100, the LRU is bounded.
    assert len(engine._fill_dedup._lru) <= 100


# ---------------------------------------------------------------------------
# N7 — conservative SELL notional (side-aware marks on the order-value gate)
# ---------------------------------------------------------------------------


def _quote_provider(ltp: str, bid: str, ask: str):
    """Quote-shaped provider: ltp/bid/ask as Price objects."""
    from types import SimpleNamespace

    def _p(v: str) -> Price:
        return Price(value=Decimal(v))

    def provider(instrument: object) -> object:
        return SimpleNamespace(ltp=_p(ltp), bid=_p(bid), ask=_p(ask))

    return provider


def _sell_request(price: str | None = None, quantity: str = "10") -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "TEST"),
        side=OrderSide.SELL,
        order_type=OrderType.MARKET if price is None else OrderType.LIMIT,
        quantity=Quantity(Decimal(quantity)),
        price=Price(Decimal(price)) if price is not None else None,
        time_in_force=TimeInForce.DAY,
    )


def test_sell_limit_below_bid_is_marked_at_bid() -> None:
    """N7: a short limit under the market must not understate the notional.

    Limit-only notional is 100 * 10 = 1000, which would pass a 1040 cap;
    the bid-side mark (105 * 10 = 1050) trips it.
    """
    rm = RiskManager(
        max_order_value=Decimal("1040"),
        price_provider=_quote_provider(ltp="104", bid="105", ask="106"),
    )
    req = _sell_request(price="100", quantity="10")
    assert rm._incoming_exposure(req) == Decimal("1050")
    assert rm.check(req) is False
    # Sanity: the naive limit-only notional would have passed the gate.
    assert Decimal("100") * req.quantity.value <= Decimal("1040")


def test_sell_limit_above_bid_keeps_limit_notional() -> None:
    """N7: a short limit above the bid uses the limit (max)."""
    rm = RiskManager(
        max_order_value=Decimal("10000"),
        price_provider=_quote_provider(ltp="104", bid="105", ask="106"),
    )
    req = _sell_request(price="110", quantity="10")
    assert rm._incoming_exposure(req) == Decimal("1100")
    assert rm.check(req) is True


def test_buy_notional_unchanged_limit_first() -> None:
    """N7: BUY keeps limit-first notional (already conservative)."""
    rm = RiskManager(
        max_order_value=Decimal("10000"),
        price_provider=_quote_provider(ltp="104", bid="105", ask="106"),
    )
    req = OrderRequest(
        instrument=Equity.of("NSE", "TEST"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("100")),
        time_in_force=TimeInForce.DAY,
    )
    assert rm._incoming_exposure(req) == Decimal("1000")
    assert rm.check(req) is True


def test_sell_market_notional_uses_bid() -> None:
    """N7: unpriced SELL market is notional-marked at the bid side."""
    rm = RiskManager(
        max_order_value=Decimal("500"),
        price_provider=_quote_provider(ltp="104", bid="105", ask="106"),
    )
    req = _sell_request(price=None, quantity="10")
    assert rm._incoming_exposure(req) == Decimal("1050")
    assert rm.check(req) is False


def test_sell_falls_back_to_limit_without_quote() -> None:
    """N7: no quote → the limit price still gates (no regression)."""
    rm = RiskManager(max_order_value=Decimal("10000"))
    req = _sell_request(price="100", quantity="10")
    assert rm._incoming_exposure(req) == Decimal("1000")
    assert rm.check(req) is True
