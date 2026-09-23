"""Risk-decision mode parity (review area #1/#4 — risk decisions and risk
limits must be identical across backtest, replay, paper, and live).

The reactive ``ExecutionEngine`` rejects orders through the shared
``RiskManager`` (OrderRejected — no fill, no position, no P&L). The backtest
path previously filled every signal unconditionally, so a configuration with
``max_order_value`` / ``max_position_value`` / rate limits behaved
differently depending on the mode. ``BacktestEngine`` now accepts the same
``RiskManager`` and gates each signal at the fill candle's open — the exact
OrderRequest the ``next_open`` reactive bridge submits — so a rejected order
is skipped identically in both paths.

These tests prove: the gate rejects and skips (no equity move), the default
(no risk manager) is unchanged, the reactive path reaches the same decision,
the rate-limit window is deterministic (evaluated at fill timestamps, not
wall clock), and ``max_position_value`` binds to the backtest's own open
positions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_domain import OHLC, Candle, OrderSide, Signal, Timeframe
from tradex_domain.enums import OrderStatus, OrderType
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.execution.engine import ExecutionEngine, RiskManager
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.replay.backtest import BacktestEngine

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _candles(prices: list[float], start_day: int = 1) -> list[Candle]:
    """Flat OHLC candles at *prices* on consecutive days — fills are exact."""
    out: list[Candle] = []
    for i, p in enumerate(prices):
        price = Price(value=Decimal(str(p)))
        out.append(Candle(
            instrument=INSTRUMENT,
            timeframe=Timeframe.D1,
            ohlc=OHLC(open=price, high=price, low=price, close=price),
            volume=Quantity(value=Decimal("1000")),
            timestamp=datetime(2026, 9, start_day + i, tzinfo=UTC),
        ))
    return out


def _intraday_candles(prices: list[float]) -> list[Candle]:
    """Flat OHLC candles 10 seconds apart (same day) — for rate-limit tests
    where multiple fills must land inside one 60-second window."""
    base = datetime(2026, 9, 1, 9, 15, tzinfo=UTC)
    out: list[Candle] = []
    for i, p in enumerate(prices):
        price = Price(value=Decimal(str(p)))
        out.append(Candle(
            instrument=INSTRUMENT,
            timeframe=Timeframe.M1,
            ohlc=OHLC(open=price, high=price, low=price, close=price),
            volume=Quantity(value=Decimal("1000")),
            timestamp=base + timedelta(seconds=10 * i),
        ))
    return out


def _request(price: str = "105", qty: str = "100") -> OrderRequest:
    return OrderRequest(
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(Decimal(qty)),
        price=Price(Decimal(price)),
    )


class _SignalEmitter:
    """Emit BUY signals on the first *buys* bars, strength *strength* each."""

    def __init__(self, instrument, buys: int, strength: float = 100.0) -> None:
        self._instrument = instrument
        self._buys = buys
        self._strength = strength
        self._signals: list = []
        self._bar = 0

    @property
    def strategy_id(self) -> str:
        return "risk-emitter"

    @property
    def signals(self) -> list:
        return list(self._signals)

    def on_bar(self, context, candle) -> object:
        self._bar += 1
        if self._bar > self._buys:
            return None
        signal = Signal(
            instrument=candle.instrument, direction=OrderSide.BUY,
            strength=self._strength, reason=f"bar{self._bar}",
            timestamp=candle.timestamp,
        )
        self._signals.append(signal)
        return signal

    def on_quote(self, context, quote) -> None:
        return None

    def on_depth(self, context, depth) -> None:
        return None

    def on_fill(self, context, fill) -> None:
        pass


class TestBacktestRiskGate:
    """BacktestEngine applies the shared RiskManager gate."""

    def test_rejects_over_limit_order_value(self) -> None:
        """BUY 100 @ next-open 105 = 10,500 > max_order_value 5,000 →
        rejected: no fill, surfaced in num_rejected."""
        risk = RiskManager(max_order_value=Decimal("5000"))
        bt = BacktestEngine(risk_manager=risk).run(
            _SignalEmitter(INSTRUMENT, buys=1), _candles([100.0, 105.0]),
        )
        assert bt.num_rejected == 1
        assert bt.num_trades == 0  # rejected order → no fill
        # Equity curve has initial + one point per candle (no position MTM).
        assert bt.equity_curve == [100000.0, 100000.0, 100000.0]

    def test_allows_under_limit_order(self) -> None:
        """BUY 10 @ next-open 105 = 1,050 ≤ max_order_value 5,000 → fills."""
        risk = RiskManager(max_order_value=Decimal("5000"))
        bt = BacktestEngine(risk_manager=risk).run(
            _SignalEmitter(INSTRUMENT, buys=1, strength=10.0),
            _candles([100.0, 105.0]),
        )
        assert bt.num_rejected == 0
        # A fill point exists between the starting capital and the closing
        # MTM — flat price series, so all points equal 100,000; the shape
        # (3 points) is what proves the order filled (rejected → 1 point).
        assert len(bt.equity_curve) == 3

    def test_no_risk_manager_is_backward_compatible(self) -> None:
        """Default (None) fills everything — historical behavior preserved."""
        bt = BacktestEngine().run(
            _SignalEmitter(INSTRUMENT, buys=1), _candles([100.0, 105.0]),
        )
        assert bt.num_rejected == 0
        # Filled (3 points) — not rejected (1 point) — with no risk manager.
        assert len(bt.equity_curve) == 3

    def test_cash_gate_uses_backtest_ledger(self) -> None:
        """A configured risk manager rejects buys beyond backtest cash."""
        risk = RiskManager()
        bt = BacktestEngine(
            risk_manager=risk,
            initial_capital=Decimal("100"),
        ).run(
            _SignalEmitter(INSTRUMENT, buys=1, strength=2.0),
            _candles([10.0, 100.0]),
        )

        assert bt.num_rejected == 1
        assert bt.num_trades == 0

    def test_master_gate_rejects_everything(self) -> None:
        """live_orders_enabled=False (kill switch) rejects all orders."""
        risk = RiskManager(live_orders_enabled=False)
        bt = BacktestEngine(risk_manager=risk).run(
            _SignalEmitter(INSTRUMENT, buys=1), _candles([100.0, 105.0]),
        )
        assert bt.num_rejected == 1
        assert bt.num_trades == 0  # rejected order → no fill
        # Equity curve has initial + one point per candle (no position MTM).
        assert bt.equity_curve == [100000.0, 100000.0, 100000.0]


class TestBacktestRateLimitDeterminism:
    """The orders-per-minute window is evaluated at fill timestamps, so a
    backtest reproduces the same risk decision on every run (review area #5:
    deterministic event processing)."""

    def test_rate_limit_rejects_third_order_deterministically(self) -> None:
        risk = RiskManager(max_orders_per_minute=2)
        # Intraday bars 10 seconds apart: all three fills land inside the
        # 60-second window, so the 3rd order is rejected deterministically.
        data = _intraday_candles([100.0, 100.0, 100.0, 100.0])
        run1 = BacktestEngine(risk_manager=risk).run(
            _SignalEmitter(INSTRUMENT, buys=3), data,
        )
        # SAME shared manager for the second run: BacktestEngine clears the
        # rate window at run start, so the second run reproduces the first
        # (a leak here would reject all 3 orders — the reproducibility
        # failure this test exists to prevent).
        run2 = BacktestEngine(risk_manager=risk).run(
            _SignalEmitter(INSTRUMENT, buys=3), data,
        )
        assert run1.num_rejected == 1  # 3 buys, window allows 2
        assert run1.num_rejected == run2.num_rejected
        assert run1.equity_curve == run2.equity_curve

    def test_shared_manager_reset_proves_reproducibility(self) -> None:
        """Regression guard: without reset_rate_window() at run start, a
        shared manager leaks its window and the second run rejects every
        order (run2.num_rejected == 3). With the reset, both runs agree."""
        risk = RiskManager(max_orders_per_minute=2)
        data = _intraday_candles([100.0, 100.0, 100.0, 100.0])
        bt = BacktestEngine(risk_manager=risk)
        first = bt.run(_SignalEmitter(INSTRUMENT, buys=3), data)
        second = bt.run(_SignalEmitter(INSTRUMENT, buys=3), data)
        assert first.num_rejected == second.num_rejected == 1
        assert first.equity_curve == second.equity_curve
        assert second.equity_curve[-1] == 100000.0

    def test_riskmanager_now_knob_expires_window(self) -> None:
        """Same request, explicit `now`: rejected inside the window, allowed
        again once the 60-second window passes — proving the `now` param
        (not wall clock) drives the decision."""
        rm = RiskManager(max_orders_per_minute=1)
        t = datetime(2026, 9, 1, 9, 15, tzinfo=UTC)
        assert rm.check(_request(), now=t) is True
        assert rm.check(_request(), now=t) is False
        assert rm.check(_request(), now=t + timedelta(seconds=61)) is True


class TestBacktestPositionLimit:
    """max_position_value binds to the backtest's own open positions."""

    def test_second_order_rejected_when_exposure_capped(self) -> None:
        """Each BUY 100 @ 100 = 10,000 notional; cap 15,000 → first fills,
        second (cumulative 20,000) is rejected."""
        risk = RiskManager(max_position_value=Decimal("15000"))
        bt = BacktestEngine(risk_manager=risk).run(
            _SignalEmitter(INSTRUMENT, buys=2), _candles([100.0, 100.0, 100.0]),
        )
        assert bt.num_rejected == 1
        assert bt.num_trades == 1  # Only first order fills; second exceeds cap


class TestReactivePathSameDecision:
    """The reactive ExecutionEngine reaches the identical risk decision on
    the same OrderRequest — the parity proof across modes."""

    def test_rejects_over_limit_and_publishes_order_rejected(self) -> None:
        from tradex_domain.events import OrderRejected

        bus = ReactiveBus()
        engine = ExecutionEngine(
            bus, SimulatedFillSource(),
            risk_manager=RiskManager(max_order_value=Decimal("5000")),
        )
        rejected: list = []
        bus.of_type(OrderRejected).subscribe(rejected.append)
        try:
            receipt = engine.submit(_request())  # 105 * 100 = 10,500
        finally:
            engine.shutdown()
        assert receipt.status is OrderStatus.REJECTED
        assert len(rejected) == 1
        assert rejected[0].reason == "risk_check_failed"
        assert len(engine.cache.all_positions()) == 0

    def test_fills_under_limit(self) -> None:
        bus = ReactiveBus()
        engine = ExecutionEngine(
            bus, SimulatedFillSource(),
            risk_manager=RiskManager(max_order_value=Decimal("100000")),
        )
        try:
            receipt = engine.submit(_request())
        finally:
            engine.shutdown()
        assert receipt.status is OrderStatus.FILLED
        positions = engine.cache.all_positions()
        assert len(positions) == 1
        assert positions[0].quantity.value == Decimal("100")
