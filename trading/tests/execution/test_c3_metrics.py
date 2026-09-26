"""Wave C3 — remaining feed/execution metrics.

TDD coverage for:
  - orders.rejection_reason.* (per-gate reason counters)
  - orders.unknown_outcome_total
  - reconciliation_drift_total.*
  - exposure_notional / daily_loss / drawdown_pct (gauges via RiskManager)
  - mark_age_seconds (gauge via MarkToMarketService)
  - feed_queue_drops_total (verified: already wired in stream.py A1)
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, OrderType, ProductType, TimeInForce
from tradex_domain.execution import Fill, OrderRequest, Position
from tradex_domain.instruments import Equity
from tradex_domain.market import Quote
from tradex_domain.value_objects import Money, OrderId, Price, Quantity

from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fill_sources import PaperFillSource
from tradex_trading.execution.mark_to_market import MarkToMarketService
from tradex_trading.execution.risk import RiskManager
from tradex_trading.execution.trading_cache import TradingCache
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.runtime.metrics import MetricsRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INST = Equity.of("NSE", "RELIANCE")
_NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)


def _req(
    side: OrderSide = OrderSide.BUY,
    qty: str = "10",
    price: str = "2500",
    instrument: Equity | None = None,
) -> OrderRequest:
    return OrderRequest(
        instrument=instrument or _INST,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal(qty)),
        price=Price(Decimal(price)),
        time_in_force=TimeInForce.DAY,
        product_type=ProductType.INTRADAY,
    )


def _fill(
    side: OrderSide = OrderSide.BUY,
    qty: str = "10",
    price: str = "100",
    ts: datetime = _NOW,
) -> Fill:
    return Fill(
        order_id=OrderId(value="fill-1"),
        instrument=_INST,
        side=side,
        quantity=Quantity(Decimal(qty)),
        price=Price(Decimal(price)),
        timestamp=ts,
    )


def _position(qty: int = 10, avg: int = 100, ts: datetime = _NOW) -> Position:
    from tradex_domain.position_math import apply_fill
    return apply_fill(None, _fill(quantity=str(qty), price=str(avg), ts=ts))


def _position_explicit(qty: int = 10, avg: int = 100) -> Position:
    return Position(
        instrument=_INST,
        quantity=Quantity(Decimal(qty)),
        avg_price=Price(Decimal(avg)),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
    )


def _quote(
    ltp: str = "110",
    bid: str | None = "109",
    ask: str | None = "111",
    ts: datetime = _NOW,
) -> Quote:
    return Quote(
        instrument=_INST,
        ltp=Price(Decimal(ltp)),
        bid=Price(Decimal(bid)) if bid else None,
        ask=Price(Decimal(ask)) if ask else None,
        timestamp=ts,
    )


def _engine(metrics: MetricsRegistry) -> ExecutionEngine:
    bus = ReactiveBus(metrics=metrics)
    return ExecutionEngine(bus=bus, fill_source=PaperFillSource(), metrics=metrics)


# ===========================================================================
# 1.  orders.rejection_reason.*  — per-gate reason counters
# ===========================================================================

class TestRejectionReasonCounters:
    """RiskManager._deny(reason) increments the right named counter."""

    def test_live_orders_disabled_reason(self) -> None:
        reg = MetricsRegistry()
        rm = RiskManager(live_orders_enabled=False, metrics=reg)
        assert rm.check(_req()) is False
        assert reg.get("orders.rejection_reason.live_orders_disabled") == 1

    def test_order_value_exceeded_reason(self) -> None:
        reg = MetricsRegistry()
        rm = RiskManager(max_order_value=Decimal("100"), metrics=reg)
        # 10 * 2500 = 25 000 > 100
        assert rm.check(_req()) is False
        assert reg.get("orders.rejection_reason.order_value_exceeded") == 1

    def test_unknown_market_value_reason(self) -> None:
        reg = MetricsRegistry()
        rm = RiskManager(
            max_order_value=Decimal("99999"),
            reject_unknown_market_value=True,
            price_provider=lambda _: None,
            metrics=reg,
        )
        # MARKET order carries no price and no mark → unknown
        mkt = OrderRequest(
            instrument=_INST,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(Decimal("1")),
            time_in_force=TimeInForce.DAY,
        )
        assert rm.check(mkt) is False
        assert reg.get("orders.rejection_reason.unknown_market_value") == 1

    def test_insufficient_cash_reason(self) -> None:
        reg = MetricsRegistry()
        rm = RiskManager(metrics=reg)
        rm.bind_cash_provider(lambda: Decimal("100"))
        # 10 * 2500 = 25 000 > 100
        assert rm.check(_req()) is False
        assert reg.get("orders.rejection_reason.insufficient_cash") == 1

    def test_position_value_exceeded_reason(self) -> None:
        reg = MetricsRegistry()
        pos = [_position_explicit(qty=10, avg=100)]
        rm = RiskManager(
            max_position_value=Decimal("500"),  # 10*100 + 1*2500 > 500
            positions_provider=lambda: pos,
            metrics=reg,
        )
        assert rm.check(_req()) is False
        assert reg.get("orders.rejection_reason.position_value_exceeded") == 1

    def test_rate_limit_exceeded_reason(self) -> None:
        reg = MetricsRegistry()
        rm = RiskManager(max_orders_per_minute=1, metrics=reg)
        ts = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
        rm.check(_req(), now=ts)          # first order passes
        result = rm.check(_req(), now=ts)  # second rejected immediately
        assert result is False
        assert reg.get("orders.rejection_reason.rate_limit_exceeded") == 1

    def test_daily_loss_exceeded_reason(self) -> None:
        reg = MetricsRegistry()
        pnl = [Decimal("0")]

        def _pnl_positions():
            return [Position(
                instrument=_INST,
                quantity=Quantity(Decimal("10")),
                avg_price=Price(Decimal("200")),
                realized_pnl=Money(amount=pnl[0], currency="INR"),
                unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
            )]

        rm = RiskManager(
            max_daily_loss_amt=Decimal("500"),
            positions_provider=_pnl_positions,
            metrics=reg,
        )
        ts = datetime(2026, 9, 24, 9, 15, tzinfo=UTC)
        # First call: pnl=0 → baseline set to 0
        rm.check(_req(), now=ts)
        # Now simulate a 2000 loss (exceeds 500 limit)
        pnl[0] = Decimal("-2000")
        result = rm.check(_req(), now=ts)
        assert result is False
        assert reg.get("orders.rejection_reason.daily_loss_exceeded") >= 1

    def test_drawdown_exceeded_reason(self) -> None:
        reg = MetricsRegistry()
        pnl = [Decimal("0")]

        def _positions():
            return [Position(
                instrument=_INST,
                quantity=Quantity(Decimal("10")),
                avg_price=Price(Decimal("100")),
                realized_pnl=Money(amount=pnl[0], currency="INR"),
                unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
            )]

        rm = RiskManager(
            max_drawdown_pct=Decimal("0.10"),
            positions_provider=_positions,
            metrics=reg,
        )
        ts = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
        # Stage 1: baseline=0, net=0, peak=0
        rm.check(_req(), now=ts)
        # Stage 2: net=1000 → peak becomes 1000
        pnl[0] = Decimal("1000")
        rm.check(_req(), now=ts)
        # Stage 3: net=800 → dd = (1000-800)/1000 = 0.2 ≥ 0.10 → deny
        pnl[0] = Decimal("800")
        result = rm.check(_req(), now=ts)
        assert result is False
        assert reg.get("orders.rejection_reason.drawdown_exceeded") >= 1

    def test_marks_not_fresh_reason(self) -> None:
        reg = MetricsRegistry()
        pos = [_position_explicit(qty=10, avg=100)]
        rm = RiskManager(
            require_fresh_marks=True,
            max_mark_age_seconds=5.0,
            positions_provider=lambda: pos,
            price_provider=lambda _: None,
            metrics=reg,
        )
        result = rm.check(_req(), now=_NOW)
        assert result is False
        assert reg.get("orders.rejection_reason.marks_not_fresh") == 1

    def test_check_order_returns_actual_reason(self) -> None:
        reg = MetricsRegistry()
        rm = RiskManager(live_orders_enabled=False, metrics=reg)
        result = rm.check_order(_req())
        assert not result.approved
        assert result.reason == "live_orders_disabled"

    def test_no_counter_emitted_when_approved(self) -> None:
        reg = MetricsRegistry()
        rm = RiskManager(metrics=reg)
        assert rm.check(_req()) is True
        snap = reg.snapshot()
        # No rejection_reason counter should exist
        assert not any(k.startswith("orders.rejection_reason") for k in snap)


# ===========================================================================
# 2.  Engine-level rejection reasons (kill_switch, feed_not_ready)
# ===========================================================================

class TestEngineRejectionReasons:
    def test_kill_switch_active_counter(self) -> None:
        reg = MetricsRegistry()
        engine = _engine(reg)
        engine.trip_kill_switch()
        engine.submit(_req())
        assert reg.get("orders.rejection_reason.kill_switch_active") >= 1

    def test_feed_not_ready_counter(self) -> None:

        reg = MetricsRegistry()
        bus = ReactiveBus(metrics=reg)

        class _NotReadySupervisor:
            ready = False

        engine = ExecutionEngine(
            bus=bus,
            fill_source=PaperFillSource(),
            metrics=reg,
            feed_supervisor=_NotReadySupervisor(),  # type: ignore[arg-type]
        )
        engine.submit(_req())
        assert reg.get("orders.rejection_reason.feed_not_ready") >= 1


# ===========================================================================
# 3.  orders.unknown_outcome_total
# ===========================================================================

class TestUnknownOutcomeTotal:
    def test_counter_incremented_on_unknown_error(self) -> None:
        reg = MetricsRegistry()
        bus = ReactiveBus(metrics=reg)

        class _BoundaryFill:
            submission_boundary_crossed = True

            def submit(self, request: OrderRequest) -> tuple:
                raise RuntimeError("network timeout after send")

        engine = ExecutionEngine(bus=bus, fill_source=_BoundaryFill(), metrics=reg)  # type: ignore[arg-type]
        from tradex_domain.errors import OrderSubmissionUnknownError
        with pytest.raises(OrderSubmissionUnknownError):
            engine.submit(_req())
        assert reg.get("orders.unknown_outcome_total") == 1

    def test_counter_not_incremented_for_normal_rejection(self) -> None:
        reg = MetricsRegistry()
        bus = ReactiveBus(metrics=reg)

        class _RejectFill:
            submission_boundary_crossed = False

            def submit(self, request: OrderRequest) -> tuple:
                raise RuntimeError("order rejected by venue")

        engine = ExecutionEngine(bus=bus, fill_source=_RejectFill(), metrics=reg)  # type: ignore[arg-type]
        engine.submit(_req())
        assert reg.get("orders.unknown_outcome_total") == 0


# ===========================================================================
# 4.  reconciliation_drift_total.*
# ===========================================================================

class TestReconciliationDriftMetrics:
    def _make_engine_with_metrics(self) -> tuple[ExecutionEngine, MetricsRegistry]:
        reg = MetricsRegistry()
        bus = ReactiveBus(metrics=reg)
        engine = ExecutionEngine(bus=bus, fill_source=PaperFillSource(), metrics=reg)
        return engine, reg

    def test_position_drift_emits_counter(self) -> None:
        engine, reg = self._make_engine_with_metrics()
        local = [_position_explicit(qty=10, avg=100)]
        broker = [_position_explicit(qty=5, avg=100)]   # qty mismatch
        drifts = engine.reconcile(broker_positions=broker, local_positions=local)
        assert len(drifts) >= 1
        # Counter key depends on severity/kind of the drift
        snap = reg.snapshot()
        drift_keys = [k for k in snap if k.startswith("reconciliation_drift_total")]
        assert len(drift_keys) >= 1

    def test_no_drift_emits_no_counter(self) -> None:
        engine, reg = self._make_engine_with_metrics()
        pos = [_position_explicit(qty=10, avg=100)]
        drifts = engine.reconcile(broker_positions=pos, local_positions=pos)
        assert drifts == []
        snap = reg.snapshot()
        assert not any(k.startswith("reconciliation_drift_total") for k in snap)

    def test_no_metrics_no_crash(self) -> None:
        bus = ReactiveBus()
        engine = ExecutionEngine(bus=bus, fill_source=PaperFillSource())
        local = [_position_explicit(qty=10, avg=100)]
        broker = [_position_explicit(qty=5, avg=100)]
        # Must not raise even without metrics
        drifts = engine.reconcile(broker_positions=broker, local_positions=local)
        assert len(drifts) >= 1

    def test_drift_counter_keyed_by_severity_and_kind(self) -> None:
        engine, reg = self._make_engine_with_metrics()
        # Position qty mismatch → LOW severity, kind="position"
        local = [_position_explicit(qty=10, avg=100)]
        broker = [_position_explicit(qty=5, avg=100)]
        engine.reconcile(broker_positions=broker, local_positions=local)
        snap = reg.snapshot()
        # At least one counter with "low" and "position"
        matching = [k for k in snap if "low" in k and "position" in k]
        assert matching, f"Expected low.position counter, got: {list(snap.keys())}"


# ===========================================================================
# 5.  exposure_notional / daily_loss / drawdown_pct gauges (RiskManager)
# ===========================================================================

class TestRiskExposureGauges:
    def test_exposure_notional_emitted_on_approved_check(self) -> None:
        reg = MetricsRegistry()
        pos = [_position_explicit(qty=10, avg=100)]
        rm = RiskManager(
            positions_provider=lambda: pos,
            price_provider=lambda _: _quote(ltp="110"),
            metrics=reg,
        )
        approved = rm.check(_req(qty="1", price="110"))
        assert approved is True
        # 10 qty * 110 mark = 1100
        assert reg.get("exposure_notional") == pytest.approx(1100.0, rel=0.01)

    def test_exposure_notional_zero_with_no_positions(self) -> None:
        reg = MetricsRegistry()
        rm = RiskManager(
            positions_provider=lambda: [],
            metrics=reg,
        )
        rm.check(_req())
        assert reg.get("exposure_notional") == 0.0

    def test_daily_loss_gauge_emitted(self) -> None:
        reg = MetricsRegistry()
        call_n = [0]

        def _pnl_positions():
            call_n[0] += 1
            pnl = Decimal("0") if call_n[0] <= 1 else Decimal("-300")
            return [Position(
                instrument=_INST,
                quantity=Quantity(Decimal("10")),
                avg_price=Price(Decimal("100")),
                realized_pnl=Money(amount=pnl, currency="INR"),
                unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
            )]

        rm = RiskManager(
            max_daily_loss_amt=Decimal("9999"),  # high so it doesn't deny
            positions_provider=_pnl_positions,
            metrics=reg,
        )
        ts = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
        rm.check(_req(), now=ts)   # seeds baseline with pnl=0
        rm.check(_req(), now=ts)   # sees pnl=-300, daily_loss should be 300
        assert reg.get("daily_loss") == pytest.approx(300.0, rel=0.01)

    def test_drawdown_pct_gauge_emitted(self) -> None:
        reg = MetricsRegistry()
        stage = [0]

        def _positions():
            pnl_by_stage = [Decimal("0"), Decimal("1000"), Decimal("800")]
            pnl = pnl_by_stage[min(stage[0], 2)]
            return [Position(
                instrument=_INST,
                quantity=Quantity(Decimal("10")),
                avg_price=Price(Decimal("100")),
                realized_pnl=Money(amount=pnl, currency="INR"),
                unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
            )]

        rm = RiskManager(
            max_drawdown_pct=Decimal("0.99"),  # high so it doesn't deny
            positions_provider=_positions,
            metrics=reg,
        )
        ts = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
        stage[0] = 0; rm.check(_req(), now=ts)   # seed baseline at 0
        stage[0] = 1; rm.check(_req(), now=ts)   # net=1000, peak=1000, dd=0
        stage[0] = 2; rm.check(_req(), now=ts)   # net=800, dd = (1000-800)/1000 = 0.2
        assert reg.get("drawdown_pct") == pytest.approx(0.2, rel=0.01)

    def test_no_gauges_without_metrics(self) -> None:
        """RiskManager without metrics must not raise."""
        rm = RiskManager(
            max_order_value=Decimal("100"),
            positions_provider=lambda: [],
        )
        assert rm.check(_req()) is False   # rejected — must not raise


# ===========================================================================
# 6.  mark_age_seconds gauge (MarkToMarketService)
# ===========================================================================

class TestMarkAgeGauge:
    def test_mark_age_zero_after_fresh_quote(self) -> None:
        reg = MetricsRegistry()
        cache = TradingCache()
        cache.update_position(_position_explicit(qty=10, avg=100))
        svc = MarkToMarketService(cache, metrics=reg)
        # quote timestamp = _NOW, called with same ts → age ≈ 0
        updated = svc.on_quote(_quote(ts=_NOW))
        assert updated is not None
        assert reg.get("mark_age_seconds") == pytest.approx(0.0, abs=0.01)

    def test_mark_age_not_emitted_for_zero_position(self) -> None:
        reg = MetricsRegistry()
        cache = TradingCache()
        # No position → on_quote returns None, no gauge
        svc = MarkToMarketService(cache, metrics=reg)
        result = svc.on_quote(_quote())
        assert result is None
        assert reg.get("mark_age_seconds") == 0.0  # never set → default 0

    def test_mark_age_no_metrics_no_crash(self) -> None:
        cache = TradingCache()
        cache.update_position(_position_explicit(qty=10, avg=100))
        svc = MarkToMarketService(cache, metrics=None)
        # Must not raise
        updated = svc.on_quote(_quote())
        assert updated is not None

    def test_mark_age_gauge_updated_on_each_quote(self) -> None:
        reg = MetricsRegistry()
        cache = TradingCache()
        cache.update_position(_position_explicit(qty=10, avg=100))
        svc = MarkToMarketService(cache, metrics=reg)
        svc.on_quote(_quote(ts=_NOW))
        first_age = reg.get("mark_age_seconds")
        # Send another quote 5s later — age should reset to 0 again
        svc.on_quote(_quote(ts=_NOW + timedelta(seconds=5), ltp="112", bid="111", ask="113"))
        second_age = reg.get("mark_age_seconds")
        assert second_age == pytest.approx(0.0, abs=0.01)
        assert first_age == pytest.approx(0.0, abs=0.01)


# ===========================================================================
# 7.  feed_queue_drops_total — verify A1 wiring (stream.py)
# ===========================================================================

def test_feed_queue_drops_counter_registered_in_stream() -> None:
    """Verify the counter name used in stream.py matches the catalog name."""
    import inspect

    import tradex_interfaces.routes.stream as stream_mod

    src = inspect.getsource(stream_mod)
    assert "feed_queue_drops_total" in src, (
        "feed_queue_drops_total counter must be registered in stream.py"
    )
    # Also verify _track_drop increments it
    assert "_track_drop" in src
    assert "_drops_counter.inc" in src


# ===========================================================================
# 8.  Prometheus render covers new metric names
# ===========================================================================

def test_rejection_reason_appears_in_prometheus_output() -> None:
    reg = MetricsRegistry()
    rm = RiskManager(max_order_value=Decimal("1"), metrics=reg)
    rm.check(_req())
    output = reg.render_prometheus()
    assert "orders_rejection_reason_order_value_exceeded" in output


def test_reconciliation_drift_appears_in_prometheus_output() -> None:
    reg = MetricsRegistry()
    bus = ReactiveBus(metrics=reg)
    engine = ExecutionEngine(bus=bus, fill_source=PaperFillSource(), metrics=reg)
    local = [_position_explicit(qty=10, avg=100)]
    broker = [_position_explicit(qty=5, avg=100)]
    engine.reconcile(broker_positions=broker, local_positions=local)
    output = reg.render_prometheus()
    assert "reconciliation_drift_total" in output


# ===========================================================================
# 9.  boot() wires the session MetricsRegistry into risk + MTM
# ===========================================================================

def test_boot_wires_metrics_into_risk_and_mtm() -> None:
    """Without this, C3 counters/gauges exist in unit tests but stay dark in paper/live."""
    from tradex_trading.config.schema import AppConfig
    from tradex_trading.runtime.startup import boot

    session = boot(AppConfig(mode="paper"))
    try:
        assert session.metrics is not None
        risk = session.engine._risk  # type: ignore[attr-defined]
        assert risk is not None
        assert risk._metrics is session.metrics
        mtm = session._mark_to_market  # type: ignore[attr-defined]
        assert mtm is not None
        assert mtm._metrics is session.metrics
    finally:
        session.stop()


def test_daily_loss_gauge_emitted_on_deny() -> None:
    """A breached daily-loss gate must still publish the gauge (not only on approve)."""
    reg = MetricsRegistry()
    pnl = [Decimal("0")]

    def _pnl_positions():
        return [Position(
            instrument=_INST,
            quantity=Quantity(Decimal("10")),
            avg_price=Price(Decimal("200")),
            realized_pnl=Money(amount=pnl[0], currency="INR"),
            unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
        )]

    rm = RiskManager(
        max_daily_loss_amt=Decimal("500"),
        positions_provider=_pnl_positions,
        metrics=reg,
    )
    ts = datetime(2026, 9, 24, 9, 15, tzinfo=UTC)
    rm.check(_req(), now=ts)  # seed baseline
    pnl[0] = Decimal("-2000")
    assert rm.check(_req(), now=ts) is False
    assert reg.get("daily_loss") == pytest.approx(2000.0, rel=0.01)
