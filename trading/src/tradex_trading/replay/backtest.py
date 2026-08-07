"""Backtest engine — runs strategies against historical data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from tradex_domain import Candle, Clock, Fill, Quote, Signal
from tradex_domain.enums import OrderSide
from tradex_domain.strategy import StrategyContext
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_trading.analytics.reports import max_drawdown, sharpe_ratio, total_return
from tradex_trading.execution.fees import FeeCalculator
from tradex_trading.strategy.core.protocols import Strategy


class FakeClock:
    """Deterministic clock — start at a fixed instant, advance per bar.

    Useful for backtesting where deterministic time progression is needed.
    """

    def __init__(self, start: datetime | None = None) -> None:
        """Initialize fake clock.

        Parameters
        ----------
        start : datetime | None
            Starting datetime. Defaults to current UTC time.
        """
        self._now = start or datetime.now(UTC)

    def now(self) -> datetime:
        """Return current time."""
        return self._now

    def advance(self, delta: timedelta) -> None:
        """Advance clock by a timedelta.

        Parameters
        ----------
        delta : timedelta
            Amount to advance.
        """
        self._now += delta


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Results from a backtest run."""

    total_return: float
    sharpe: float
    max_drawdown: float
    num_trades: int
    trades: list[Signal] = field(default_factory=list)
    total_fees: float = 0.0


class BacktestEngine:
    """Runs strategies against historical data."""

    def __init__(
        self,
        fill_source: Any | None = None,  # fill sources vary
        clock: Clock | None = None,
        fee_calculator: FeeCalculator | None = None,
    ) -> None:
        """Initialize backtest engine.

        Args:
            fill_source: Optional fill source for simulating fills
            clock: Optional FakeClock for deterministic time progression
            fee_calculator: Optional FeeCalculator; when provided, fees are
                deducted from cash on each fill and surfaced in BacktestResult.
        """
        self._fill_source = fill_source
        self._clock = clock or FakeClock()
        self._fee_calculator = fee_calculator

    def submit(self, request: Any) -> Any:
        """Route a strategy order through the backtest engine.

        Wire a strategy's order callback to this method so its orders are
        counted and filled inside the backtest.

        Parameters
        ----------
        request : OrderRequest
            The order request to submit.

        Returns
        -------
        object
            The order receipt or result.
        """
        if self._fill_source is not None and hasattr(self._fill_source, "submit"):
            return self._fill_source.submit(request)  # type: ignore[attr-defined]
        return request

    def run(self, strategy: Strategy, data: list[Candle | Quote | Fill]) -> BacktestResult:
        """Run a backtest.

        Feed data events to the strategy, then compute real P&L from the
        emitted signals using each signal's matching Candle close as fill price.

        Args:
            strategy: Strategy instance
            data: List of events (Candle, Quote, Fill)

        Returns:
            BacktestResult with performance metrics
        """
        initial_capital = Decimal("100000")
        cash = initial_capital
        # positions: instrument_id -> (quantity, avg_price)
        positions: dict = {}

        # Collect candles per instrument_id (preserving order for next-match lookup)
        candles_by_id: dict = {}
        for event in data:
            if isinstance(event, Candle):
                candles_by_id.setdefault(event.instrument.instrument_id, []).append(event)

        # Feed events to strategy
        ctx = StrategyContext()
        bar_count = 0
        for event in data:
            if isinstance(event, Candle):
                bar_count += 1
                ctx = StrategyContext(bar_count=bar_count, timestamp=event.timestamp)
                strategy.on_bar(ctx, event)
            elif isinstance(event, Quote):
                ctx = StrategyContext(timestamp=event.timestamp)
                strategy.on_quote(ctx, event)
            elif isinstance(event, Fill):
                strategy.on_fill(ctx, event)

        signals = getattr(strategy, "signals", [])
        num_trades = len(signals)

        # Guard: no signals → zeroed metrics
        if not signals:
            return BacktestResult(
                total_return=0.0,
                sharpe=0.0,
                max_drawdown=0.0,
                num_trades=0,
                trades=[],
                total_fees=0.0,
            )

        # Build equity curve from signal-driven trades.
        # Signals carry no timestamp, so we match them sequentially against the
        # candle stream for each instrument: signal N consumes candle N.
        equity_curve_dec: list[Decimal] = [initial_capital]
        cursor_by_id: dict = {}
        total_fees = Decimal("0")
        fee_seq = 0

        for signal in signals:
            inst_id = signal.instrument.instrument_id
            inst_candles = candles_by_id.get(inst_id, [])
            cursor = cursor_by_id.get(inst_id, 0)
            if cursor >= len(inst_candles):
                # No candle available for this signal — counted in num_trades
                # but does not move equity.
                continue
            fill_candle = inst_candles[cursor]
            cursor_by_id[inst_id] = cursor + 1

            price = fill_candle.ohlc.close.value
            if not isinstance(price, Decimal):
                price = Decimal(str(price))

            # Infer quantity from signal strength, default 1
            strength = getattr(signal, "strength", None)
            qty = (
                Decimal(str(strength))
                if strength is not None and strength != 0
                else Decimal("1")
            )
            if qty < 0:
                qty = -qty

            fee = Decimal("0")
            if self._fee_calculator is not None and price > 0 and qty > 0:
                fee_seq += 1
                domain_fill = Fill(
                    order_id=OrderId(f"bt-{fee_seq}"),
                    instrument=signal.instrument,
                    side=signal.direction,
                    quantity=Quantity(qty),
                    price=Price(price),
                    timestamp=fill_candle.timestamp,
                )
                fee = self._fee_calculator.calculate(domain_fill).amount
                total_fees += fee
                cash -= fee

            if signal.direction == OrderSide.BUY:
                cost = price * qty
                cash -= cost
                prev_qty, prev_avg = positions.get(inst_id, (Decimal("0"), Decimal("0")))
                new_qty = prev_qty + qty
                new_avg = (
                    (prev_avg * prev_qty + price * qty) / new_qty
                    if new_qty > Decimal("0")
                    else price
                )
                positions[inst_id] = (new_qty, new_avg)
                equity_curve_dec.append(cash + self._mark_to_market(positions, candles_by_id))

            elif signal.direction == OrderSide.SELL:
                prev_qty, prev_avg = positions.get(inst_id, (Decimal("0"), Decimal("0")))
                sell_qty = min(qty, prev_qty)
                if sell_qty <= Decimal("0"):
                    continue
                proceeds = price * sell_qty
                cash += proceeds
                new_qty = prev_qty - sell_qty
                if new_qty > Decimal("0"):
                    positions[inst_id] = (new_qty, prev_avg)
                else:
                    positions.pop(inst_id, None)
                equity_curve_dec.append(cash + self._mark_to_market(positions, candles_by_id))

        # Build float equity curve and returns
        equity_curve = [float(v) for v in equity_curve_dec]
        returns: list[float] = []
        for i in range(1, len(equity_curve)):
            prev = equity_curve[i - 1]
            returns.append((equity_curve[i] - prev) / prev if prev != 0.0 else 0.0)

        return BacktestResult(
            total_return=total_return(equity_curve),
            sharpe=sharpe_ratio(returns),
            max_drawdown=max_drawdown(equity_curve),
            num_trades=num_trades,
            trades=signals,
            total_fees=float(total_fees),
        )

    @staticmethod
    def _mark_to_market(positions: dict, candles_by_id: dict) -> Decimal:
        """Sum position qty * last close price across all open positions."""
        mtm = Decimal("0")
        for inst_id, (qty, _avg) in positions.items():
            if qty <= Decimal("0"):
                continue
            inst_candles = candles_by_id.get(inst_id, [])
            if not inst_candles:
                continue
            last_close = inst_candles[-1].ohlc.close.value
            if not isinstance(last_close, Decimal):
                last_close = Decimal(str(last_close))
            mtm += qty * last_close
        return mtm


__all__ = ["BacktestEngine", "BacktestResult", "FakeClock"]
