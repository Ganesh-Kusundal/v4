"""Backtest engine — runs strategies against historical data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from tradex_domain import Candle, Clock, Fill, Quote, Signal
from tradex_domain.enums import OrderStatus, OrderType, Timeframe
from tradex_domain.events import OrderFilled, PlaceOrderCommand
from tradex_domain.execution import OrderRequest
from tradex_domain.utils import q2
from tradex_domain.value_objects import CorrelationId, Price, Quantity

from tradex_trading.analytics.reports import max_drawdown, sharpe_ratio, total_return
from tradex_trading.datalake.corporate_actions import CorporateActionStore
from tradex_trading.execution.cash_ledger import CashLedger
from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fees import FeeCalculator
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.execution.position_manager import PositionManager
from tradex_trading.execution.slippage import SlippageModel
from tradex_trading.execution.trading_cache import TradingCache
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.strategy.core.engine import ReactiveStrategyEngine
from tradex_trading.strategy.core.protocols import Strategy


def _parse_ex_date(value: str) -> date:
    """Parse a corporate-action ISO ex-date string to a ``date``."""
    return date.fromisoformat(value)


def _action_identity(action: Any) -> str:
    """Stable identity for an action: ex-date + type + amount + ratio.

    Two distinct actions sharing one ex-date (e.g. a dividend and a split on
    the same day) must be applied separately, so the once-per-run dedup key
    is never the bare ex-date string.
    """
    return f"{action.ex_date}|{action.action_type}|{action.amount}|{action.ratio}"


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
    #: Signals emitted by the strategy — INCLUDING any the risk manager
    #: rejected and any with no bar to fill at (a signal for an instrument
    #: that never appears in the data never trades). Metrics are computed
    #: solely from ``equity_curve`` (which only moves on actual fills), so
    #: return/sharpe/drawdown are unaffected by rejected or unmatched
    #: signals; use ``num_rejected`` to see how many the risk gate
    #: suppressed.
    num_trades: int
    #: Emitted signals (see ``num_trades``) — the full audit trail, not a
    #: list of executed fills.
    trades: list[Signal] = field(default_factory=list)
    total_fees: float = 0.0
    equity_curve: list[float] = field(default_factory=list)
    #: Signals whose orders failed the configured risk manager and were
    #: skipped (no fill, no P&L) — the backtest mirror of OrderRejected.
    num_rejected: int = 0


class BacktestEngine:
    """Runs strategies against historical data."""

    def __init__(
        self,
        fill_source: Any | None = None,  # fill sources vary
        clock: Clock | None = None,
        fee_calculator: FeeCalculator | None = None,
        slippage_model: SlippageModel | None = None,
        corporate_actions: CorporateActionStore | None = None,
        risk_manager: Any | None = None,
        initial_capital: Decimal | float | str = Decimal("100000"),
        sharpe_frequency: str = "1m",
    ) -> None:
        """Initialize backtest engine.

        Args:
            fill_source: Optional fill source for simulating fills
            clock: Optional FakeClock for deterministic time progression
            fee_calculator: Optional FeeCalculator; when provided, fees are
                deducted from cash on each fill and surfaced in BacktestResult.
            slippage_model: Optional slippage model; when provided, the fill
                price is worsened by the model on each trade (BUY pays more,
                SELL receives less). Defaults to no slippage.
            corporate_actions: Optional ``CorporateActionStore``; when
                provided, SPLIT/BONUS/DIVIDEND actions whose ex-date falls on
                or before a fill bar are applied to the open position at that
                point — point-in-time, once each, through the same shared
                accounting math as ``PositionManager`` (parity review area
                #4: corporate actions modeled consistently across modes).

        Note: fees are charged on the *applied* fill — a SELL larger than the
        open position is capped at the position, so the fee matches the
        actual (capped) quantity, and a SELL with no position to close pays
        no fee.

        risk_manager:
            Optional shared ``RiskManager`` (the same class the reactive
            ``ExecutionEngine`` uses). When provided, each signal's order is
            risk-checked at the fill candle's open — the same request the
            ``next_open`` reactive bridge submits — and a rejected order is
            skipped entirely (no fill, no P&L), mirroring OrderRejected in
            paper/live (parity review area #1/#4: risk decisions consistent
            across modes). ``max_position_value`` binds to the backtest's own
            open positions when the manager has no explicit provider; the
            orders-per-minute window is evaluated at the fill timestamp for
            determinism. Defaults to None (no risk gate — historical behavior).

            Dedicate the manager to this engine: ``run()`` binds its
            positions provider and resets its rate window, so concurrent
            runs sharing one manager would race on the provider binding, and
            its rate-limit state belongs to the backtest, not live order flow.
        """
        self._fill_source = fill_source
        self._clock = clock or FakeClock()
        self._fee_calculator = fee_calculator
        self._slippage_model = slippage_model
        self._corporate_actions = corporate_actions
        self._risk_manager = risk_manager
        self._initial_capital = Decimal(str(initial_capital))
        self._sharpe_frequency = sharpe_frequency

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
        initial_capital = self._initial_capital

        # Snapshot signals already on the strategy (e.g. a strategy object
        # reused from a prior ReplayEngine run) so this run only counts the
        # signals it emits itself — never pre-existing ones (parity area #5).
        _pre_signals = len(getattr(strategy, "signals", []) or [])

        # --- Unified reactive pipeline (same as live/replay/paper) -----------
        # Backtest is now a *bus driver*: it publishes historical events onto a
        # ReactiveBus; the strategy (via ReactiveStrategyEngine, next_open) emits
        # PlaceOrderCommand at the next bar's open; the SAME ExecutionEngine +
        # PositionManager + FeeCalculator + RiskManager that live uses processes
        # the order. Fills/risk/fees/positions are therefore computed by one code
        # path in every mode (parity review CRITICAL-1). Backtest-specific
        # reporting (cash ledger, point-in-time MTM, sharpe/drawdown) is derived
        # from PositionManager + CashLedger state after the stream completes.
        bus = ReactiveBus()
        cache = TradingCache()
        position_manager = PositionManager(cache)
        fill_source = self._fill_source or SimulatedFillSource(
            slippage_model=self._slippage_model,
        )
        engine = ExecutionEngine(
            bus,
            fill_source,
            risk_manager=self._risk_manager,
            cache=cache,
            fee_calculator=self._fee_calculator,
        )
        ledger = CashLedger(initial_capital)

        # Cash ledger subscribes to fills (orchestrated cash tracking).
        def _on_fill(ev: OrderFilled) -> None:
            f = ev.fill
            ledger.on_fill(f.side, f.quantity, f.price)
            if self._fee_calculator is not None:
                ledger.on_fee(self._fee_calculator.calculate(f).amount)

        bus.of_type(OrderFilled).subscribe(_on_fill)

        # Strategy engine (next_open) drives PlaceOrderCommand at next-open.
        strategy_engine = ReactiveStrategyEngine(bus, fill_reference="next_open")
        strategy_engine.register(strategy)

        # Risk manager binds to the SAME positions the engine fills, and resets
        # its rate window so each run is reproducible (parity area #8).
        if self._risk_manager is not None:
            if not getattr(self._risk_manager, "positions_provider_bound", False):
                self._risk_manager.set_positions_provider(
                    lambda: list(cache.all_positions()),
                )
            self._risk_manager.reset_rate_window()

        # Corp actions: applied to the shared PositionManager (and the ledger's
        # cash effects mirrored) at each bar's ex-date — before that bar's fill.
        applied_actions: dict[str, set[str]] = {}
        def _apply_ca_for(ts: datetime) -> None:
            if self._corporate_actions is None:
                return
            for pos in list(cache.all_positions()):
                self._apply_actions_due(
                    pos.instrument, position_manager, ledger, ts, applied_actions,
                )

        # Collect candles per instrument_id for point-in-time MTM.
        candles_by_id: dict[Any, list] = {}
        for event in data:
            if isinstance(event, Candle):
                candles_by_id.setdefault(event.instrument.instrument_id, []).append(event)

        # Stamp the strategy version onto each signal's metadata (audit trail).
        # The strategy is fed events through the bus; signals are collected
        # from the strategy object after the stream (parity area #5).
        version = str(getattr(strategy, "version", "1.0.0"))

        # --- Recorded-signal bridge (parity with the next_open engine) --------
        # A strategy that RECORDS signals (appends to ``strategy.signals``)
        # instead of returning them from its callbacks never reaches the
        # strategy engine — the engine only bridges signals that ``on_bar`` /
        # ``on_quote`` RETURN. Track which signals the engine has claimed
        # (returned + queued for next_open) so this bridge publishes orders
        # ONLY for signals the engine has not already claimed: a strategy that
        # returns signals keeps its orders flowing through the engine exactly
        # as before (no double-counting), while a recording-only strategy's
        # signals still reach the unified pipeline and produce fills.
        # M3 — Fill timing: recording-only bridge fills at this bar's close
        # (legacy sequential match); returning signals fill at the *next* bar's
        # open via ReactiveStrategyEngine. Recording vs returning therefore
        # determines fill timing — callers must not mix both for one signal.
        claimed_signal_ids: set[int] = set()
        bridged_signal_ids: set[int] = set()
        bridge_seq = 0

        def _capture_claimed() -> None:
            for pending in strategy_engine.pending_snapshot():
                claimed_signal_ids.add(id(pending["signal"]))

        def _bridge_signal(signal: Signal, price: Price, ts: datetime | None) -> None:
            nonlocal bridge_seq
            bridge_seq += 1
            qty_value = abs(signal.strength) if signal.strength else 1.0
            request = OrderRequest(
                instrument=signal.instrument,
                side=signal.direction,
                order_type=OrderType.MARKET,
                quantity=Quantity(Decimal(str(qty_value))),
                price=price,
                correlation_id=CorrelationId(value=f"backtest-bridge-{bridge_seq}"),
                tag=f"{getattr(strategy, 'strategy_id', 'strategy')}@{version}",
                reference_timestamp=ts,
            )
            bridged_signal_ids.add(id(signal))
            bus.publish(PlaceOrderCommand(request=request))

        # Stream historical events through the unified pipeline.
        equity_curve_dec: list[Decimal] = [initial_capital]
        for event in data:
            if isinstance(event, Candle):
                # Apply due corporate actions BEFORE the bar's open fill.
                _apply_ca_for(event.timestamp)
                bus.publish(event)
                _capture_claimed()
                # Recording-only strategies: bridge their next unmatched signal
                # at this candle's close (legacy sequential signal→candle
                # matching, "signal N consumes candle N"). Never bridge once the
                # engine is driving orders from returned signals (next_open) —
                # its fill at the next bar's open would then double-count.
                if not claimed_signal_ids:
                    for signal in getattr(strategy, "signals", []) or []:
                        if (
                            id(signal) in bridged_signal_ids
                            or signal.instrument.instrument_id
                            != event.instrument.instrument_id
                        ):
                            continue
                        _bridge_signal(
                            signal,
                            Price(value=Decimal(str(event.ohlc.close.value))),
                            event.timestamp,
                        )
                        break
                # Point-in-time MTM at this bar's close.
                positions_now = {
                    p.instrument.instrument_id: p for p in cache.all_positions()
                }
                equity_curve_dec.append(
                    ledger.cash
                    + self._mark_to_market(positions_now, candles_by_id, event.timestamp)
                )
            elif isinstance(event, Quote):
                bus.publish(event)
                _capture_claimed()
            elif isinstance(event, Fill):
                bus.publish(event)
                _capture_claimed()

        # --- Flush orders deferred past the last bar (next_open) --------------
        # next_open fills a signal at the FOLLOWING candle's open, so a signal
        # emitted on the final event (e.g. a quote after the last candle) would
        # otherwise be dropped by the engine's dispose. Flush deferred engine
        # orders at the last known bar, then bridge any recorded signal that was
        # never returned by the strategy and never matched during the stream.
        last_candle_by_id = {
            inst_id: candles[-1] for inst_id, candles in candles_by_id.items()
        }
        for inst_id in {p["instrument_id"] for p in strategy_engine.pending_snapshot()}:
            candle = last_candle_by_id.get(inst_id)
            if candle is not None:
                strategy_engine.flush_pending(candle)
        for signal in getattr(strategy, "signals", []) or []:
            if id(signal) in bridged_signal_ids or id(signal) in claimed_signal_ids:
                continue
            candle = last_candle_by_id.get(signal.instrument.instrument_id)
            if candle is None:
                continue
            _bridge_signal(
                signal,
                Price(value=Decimal(str(candle.ohlc.close.value))),
                candle.timestamp,
            )

        engine.shutdown()
        strategy_engine.dispose_all()

        # --- Derive BacktestResult from pipeline state (unchanged fields) -----
        _all_signals = getattr(strategy, "signals", []) or []
        signals = _all_signals[_pre_signals:]
        for signal in signals:
            meta = getattr(signal, "metadata", None)
            if isinstance(meta, dict):
                meta.setdefault("strategy_version", version)

        filled_orders = [
            o for o in cache.all_orders()
            if o.status == OrderStatus.FILLED
        ]
        num_trades = len(filled_orders)
        rejected = (
            self._risk_manager.rejected_count
            if self._risk_manager is not None else 0
        )
        total_fees = ledger.total_fees

        # Build float equity curve and returns.
        equity_curve = [float(v) for v in equity_curve_dec]
        returns: list[float] = []
        for i in range(1, len(equity_curve)):
            prev = equity_curve[i - 1]
            returns.append((equity_curve[i] - prev) / prev if prev != 0.0 else 0.0)

        # C3: infer Sharpe frequency from candle timeframe when default "1m" is left on D1 data
        frequency = self._sharpe_frequency
        if frequency == "1m" and data:
            try:
                first = data[0]
                if hasattr(first, "timeframe"):
                    tf = getattr(first, "timeframe", None)
                    if tf is not None:
                        mapping = {
                            Timeframe.D1: "daily",
                            Timeframe.M1: "1m",
                            Timeframe.M5: "5m",
                            Timeframe.M15: "15m",
                            Timeframe.H1: "hour",
                            Timeframe.M30: "30m",
                            Timeframe.W1: "weekly",
                        }
                        inferred = mapping.get(tf)  # type: ignore[arg-type]
                        if inferred is not None and inferred != "1m":
                            frequency = inferred
            except Exception:
                pass

        return BacktestResult(
            total_return=total_return(equity_curve),
            sharpe=sharpe_ratio(returns, frequency=frequency),
            max_drawdown=max_drawdown(equity_curve),
            num_trades=num_trades,
            trades=signals,
            total_fees=float(total_fees),
            equity_curve=equity_curve,
            num_rejected=rejected,
        )

    def _apply_actions_due(
        self,
        instrument: Any,
        position_manager: Any,
        ledger: Any,
        as_of: datetime | None,
        applied: dict[str, set[str]],
    ) -> None:
        """Apply corporate actions with ex-date <= *as_of* to the shared
        PositionManager (and mirror their cash effects onto the ledger).

        Point-in-time: only actions already effective by *as_of* are applied,
        in ex-date order, and each action exactly once per run. The position
        math is delegated to ``PositionManager.on_corporate_action`` — the same
        ``apply_split`` / ``apply_dividend`` model the reactive pipeline uses
        (parity review area #4), so backtest, replay, paper, and live book
        corporate actions identically. The cash ledger receives the same basis
        restatement (split) and per-share credit (dividend) the legacy private
        loop applied, so realized P&L and equity stay equal to the rupee.
        """
        if as_of is None or self._corporate_actions is None:
            return
        actions = [
            a
            for a in self._corporate_actions.get_typed(instrument.symbol)
            if a.ex_date and _parse_ex_date(a.ex_date) <= as_of.date()
        ]
        if not actions:
            return
        actions.sort(key=lambda a: a.ex_date)
        done = applied.setdefault(instrument.instrument_id, set())
        # Read the open position BEFORE applying (split re-bases qty; dividend
        # credit is per pre-action qty).
        existing = position_manager._cache.get_position(instrument)
        for action in actions:
            identity = _action_identity(action)
            if identity in done:
                continue
            done.add(identity)
            if existing is None:
                continue  # no open position — nothing to adjust
            kind = action.action_type.upper()
            if kind in ("SPLIT", "BONUS") and action.ratio > 0:
                old_basis = existing.avg_price.value * existing.quantity.value
                position_manager.on_corporate_action(
                    instrument, kind, ratio=action.ratio,
                )
                existing = position_manager._cache.get_position(instrument)
                new_basis = existing.avg_price.value * existing.quantity.value
                ledger.restate(old_basis - new_basis)
            elif kind == "DIVIDEND":
                per_share = Decimal(str(action.amount))
                ledger.credit(q2(per_share * existing.quantity.value))
                position_manager.on_corporate_action(
                    instrument, "DIVIDEND", per_share=action.amount,
                )
                existing = position_manager._cache.get_position(instrument)

    @staticmethod
    def _mark_to_market(
        positions: dict,
        candles_by_id: dict,
        as_of: datetime | None,
    ) -> Decimal:
        """Sum position qty * latest close known at *as_of* across open positions.

        Point-in-time: only candles with ``timestamp <= as_of`` are used, so a
        position is never marked at a close from the future. Untimestamped
        (legacy) signals pass ``as_of=None`` and fall back to the full series.
        Positions are the shared-model ``Position`` objects.

        Both long and short positions are marked: long positions contribute
        ``qty * close`` (positive), short positions contribute
        ``qty * close`` (negative, since qty is negative) — so the MTM
        correctly reflects the mark-to-market P&L of short holdings.

        Instruments without candles contribute 0 MTM (intentional — no mark
        available); such positions are held at cost until a candle arrives.
        """
        mtm = Decimal("0")
        for inst_id, pos in positions.items():
            qty = pos.quantity.value
            if qty == Decimal("0"):
                continue
            inst_candles = candles_by_id.get(inst_id, [])
            if not inst_candles:
                continue
            if as_of is not None:
                known = [c for c in inst_candles if c.timestamp <= as_of]
                if not known:
                    continue
                last_close = known[-1].ohlc.close.value
            else:
                last_close = inst_candles[-1].ohlc.close.value
            if not isinstance(last_close, Decimal):
                last_close = Decimal(str(last_close))
            mtm += qty * last_close
        return mtm


__all__ = ["BacktestEngine", "BacktestResult", "FakeClock"]
