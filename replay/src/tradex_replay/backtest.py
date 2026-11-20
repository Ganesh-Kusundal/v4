"""Backtest engine — runs strategies against historical data."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, cast

from tradex_analytics.reports import (
    max_drawdown,
    periods_per_year,
    sharpe_ratio,
    total_return,
)
from tradex_analytics.trade_metrics import compute_statistics
from tradex_analytics.validation_report import round_trips_from_fills
from tradex_domain import Candle, Clock, Fill, Quote, Signal
from tradex_domain.constants import DEFAULT_INITIAL_CAPITAL
from tradex_domain.correlation_id import mint_correlation_id
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, Timeframe
from tradex_domain.events import (
    ErrorOccurred,
    OrderFilled,
    OrderPlaced,
    OrderRejected,
)
from tradex_domain.execution import OrderRequest
from tradex_domain.protective import stop_hit, target_hit
from tradex_domain.utils import q2
from tradex_domain.value_objects import CorrelationId, Money, Price, Quantity
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fees import FeeCalculator
from tradex_execution.fill_sources import FillSource, SimulatedFillSource
from tradex_execution.mark_to_market import MarkToMarketService
from tradex_execution.position_accountant import PositionAccountant
from tradex_execution.slippage import SlippageModel
from tradex_execution.trading_cache import TradingCache
from tradex_market_data.corporate_actions import CorporateActionStore
from tradex_reactive.bus import ReactiveBus
from tradex_strategy.core.brackets import is_valid_bracket, protective_request
from tradex_strategy.core.engine import ReactiveStrategyEngine
from tradex_strategy.core.protocols import Strategy
from tradex_strategy.core.sizing import DEFAULT_MAX_ORDER_VALUE, PositionSizer

log = logging.getLogger(__name__)

#: Adverse fill beyond a triggered stop, in basis points (AC-19). **This is a
#: stated assumption, not a measurement.** 25 bps (0.25%) is the conservative
#: end of what an intraday stop-market on a liquid NSE equity actually slips
#: when a sharp move triggers it — several ticks, more on a thin name. It is
#: deliberately not zero: a stop honoured at exactly its own price is an
#: optimism no venue grants, and a strategy whose edge survives only because its
#: stop is always filled precisely has been measured on a fiction.
#:
#: It is a *floor* on realism, not a replacement for the bar — see
#: :func:`_stop_gap_price`, which bounds the gap by the bar's own adverse
#: extreme so a modelled gap can never invent a loss the market did not print,
#: and (just as important) can never beat a fill the market *did* print.
DEFAULT_STOP_GAP_BPS: Final = Decimal("25")

#: A fill price must stay positive: a zero- or negative-priced fill silently
#: corrupts every downstream P&L number and is refused by the fill model.
_MIN_FILL_PRICE: Final = Decimal("0.01")

#: Fraction of the run's opening capital one trade may risk, used **only** when
#: the caller wires no sizer of its own (AC-5). **A stated default, not a spec
#: value** — the spec fixes the sizing formula and not the fraction, and the same
#: 1% convention appears in the tradex-platform account wiring. It only ever
#: applies where the risk denominator is knowable at all (a declared stop *and* a
#: lot size); everywhere else the sizer takes the configured notional and says
#: so in the log. Pass ``sizer=`` for anything load bearing.
DEFAULT_BACKTEST_RISK_FRACTION: Final = Decimal("0.01")


@dataclass(slots=True)
class _OpenBracket:
    """Engine-managed protective exit for one open entry fill.

    ``stop``/``target`` are the *current* protective levels (a trailing stop
    mutates ``stop`` as the market moves). ``trailing_jump`` is the trail
    distance when the strategy declared one; ``high_water``/``low_water`` track
    the favourable extreme reached since entry so the stop can ratchet.
    """

    instrument: Any
    side: OrderSide
    quantity: Quantity
    stop: float
    target: float | None
    trailing_jump: float | None = None
    high_water: float = 0.0
    low_water: float = 0.0


def _stop_gap_price(
    kind: str,
    entry_side: OrderSide,
    level: float,
    bar_low: float,
    bar_high: float,
    gap_bps: Decimal = DEFAULT_STOP_GAP_BPS,
) -> float:
    """Price a triggered protective leg actually fills at (AC-19).

    *gap_bps* is in the same unit as :data:`DEFAULT_STOP_GAP_BPS` — basis
    points — so the public knob and the arithmetic cannot drift apart.

    **The stop is a stop-*market***: the instant a print is through the level the
    order is live, and the fill is the next price actually available — which is
    worse than the level whenever the bar moved through it. The old model
    returned the level itself, so a long stopped at 1,000.00 filled at 1,000.00
    on a bar that printed 980.00: a loss the market would not have allowed, and
    invisible because the number has the same shape as the right one.

    Two terms, and the **worse** one wins:

    * the bar's own adverse extreme — the best price the venue could possibly
      have given, so a bar that genuinely gapped through reports what it
      printed rather than a modelled approximation of it. This is arithmetic,
      not a modelling choice, so it always applies;
    * the adverse gap, ``level * (1 -/+ gap)`` — the slippage past the trigger,
      which ``gap`` controls. A zero gap is still sound (the bar bound alone
      cannot beat the stop) and is what the negative controls switch off; a
      *negative* gap would be a request to fill better than the stop, and
      ``BacktestEngine`` refuses it at construction.

    The second term is what the first is for. A gap model with no bar bound
    manufactures losses on bars that merely traded through the level; a bar
    bound with no gap model is the defect AC-19 removes.

    **A take-profit is a limit order and does not gap** — the venue fills it at
    the limit or better whenever the bar reaches the level — so ``kind ==
    "target"`` returns the level untouched. When both legs pierce one bar the
    caller's stop-first convention still decides which leg this prices.
    """
    if kind != "stop":
        return level
    gap = Decimal(str(gap_bps)) / Decimal("10000")
    level_d = Decimal(str(level))
    if entry_side is OrderSide.BUY:
        # Long: the exit sells, so the adverse direction is down.
        adverse = min(level_d * (Decimal(1) - gap), Decimal(str(bar_low)))
    else:
        # Short: the exit buys back, so the adverse direction is up.
        adverse = max(level_d * (Decimal(1) + gap), Decimal(str(bar_high)))
    return float(max(adverse, _MIN_FILL_PRICE))


def adv_provider_from_candles(
    candles_by_id: dict[Any, list], lookback: int = 20
) -> Callable[[Any, datetime | None], Decimal]:
    """Build an ADV provider for :class:`MarketImpactSlippageModel`.

    Returns ``(instrument, timestamp) -> Decimal`` giving the mean per-bar
    volume over the last *lookback* bars up to *timestamp* (the whole series
    when *timestamp* is ``None``). The result is a liquidity proxy in the run's
    own timeframe, so ``quantity / adv`` in the impact model reads as a fraction
    of a typical bar's volume. Pass the returned callable as
    ``MarketImpactSlippageModel(adv_provider=...)`` to ``BacktestEngine``.
    """

    def provider(instrument: Any, timestamp: datetime | None) -> Decimal:
        iid = getattr(instrument, "instrument_id", instrument)
        bars = candles_by_id.get(iid, [])
        if timestamp is not None:
            bars = [b for b in bars if b.timestamp <= timestamp]
        window = bars[-lookback:] if lookback > 0 else bars
        if not window:
            return Decimal("0")
        total = sum((Decimal(str(b.volume.value)) for b in window), Decimal("0"))
        return total / Decimal(len(window))

    return provider


def _bar_volume_provider(
    candles_by_id: dict[Any, list],
) -> Callable[[Any, datetime | None], Decimal]:
    """Build a ``(instrument, timestamp) -> Decimal`` traded-volume lookup.

    Returns the volume of the bar at or before *timestamp* (the bar an order
    fills against), or the last known bar when *timestamp* is ``None``. Used as
    the liquidity cap for P0.2 volume-capped partial fills.
    """

    def provider(instrument: Any, timestamp: datetime | None) -> Decimal:
        iid = getattr(instrument, "instrument_id", instrument)
        bars = candles_by_id.get(iid, [])
        if not bars:
            return Decimal("0")
        if timestamp is None:
            return Decimal(str(bars[-1].volume.value))
        chosen = None
        for bar in bars:
            if bar.timestamp <= timestamp:
                chosen = bar
            else:
                break
        return Decimal(str(chosen.volume.value)) if chosen is not None else Decimal("0")

    return provider


def _protective_trigger(
    candle: Candle,
    side: OrderSide,
    stop: float,
    target: float | None,
) -> tuple[str | None, float | None]:
    """Return ``(kind, level)`` when the candle pierces a declared leg.

    ``level`` is the declared level the bar reached, **not** the price the leg
    fills at — a triggered stop is a stop-market and fills past its own level
    (AC-19). Price it with :func:`_stop_gap_price`.

    Stop is tested before target when both pierce on one bar — the same
    pessimistic convention as ``BracketBreakoutStrategy._exit_for_bar``.

    *target* may be ``None`` (a trailing-only protection with no take-profit),
    in which case only the stop is tested.
    """
    stop_hit_result = stop_hit(candle, Price(value=Decimal(str(stop))), side)
    if target is None:
        return ("stop", stop) if stop_hit_result else (None, None)
    target_hit_result = target_hit(candle, Price(value=Decimal(str(target))), side)
    if not stop_hit_result and not target_hit_result:
        return (None, None)
    if stop_hit_result:
        return ("stop", stop)
    return ("target", target)


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

    def set(self, instant: datetime) -> None:
        """Jump to an absolute instant (e.g. the bar under evaluation)."""
        self._now = instant

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
    #: Signals the run could not fill because the bar they must fill against
    #: never arrived — a signal on the final bar, or on a quote after the last
    #: candle. Reported rather than filled at a price that did not exist when
    #: the strategy decided (AC-1). They stay in ``trades``: the signal was
    #: emitted, it simply had no bar to trade on.
    num_unfilled: int = 0
    #: Executed fills recorded by the pipeline (the ``OrderFilled`` events the
    #: execution engine applied to the position book and the cash ledger).
    #: Unlike ``trades`` these carry real fill prices/timestamps; chart markers
    #: should plot THESE. Rejections appear
    #: here with ``rejected=True`` and price 0 so a UI can render them as
    #: error markers without consulting ``num_rejected`` arithmetic.
    fills: list[dict[str, Any]] = field(default_factory=list)
    #: The position book the run ended on, one dict per instrument that is
    #: still open, carrying ``instrument_id``, ``symbol``, ``quantity``,
    #: ``avg_price`` and ``realized_pnl``. Reported because a corporate action
    #: is otherwise invisible in a run's output: the only evidence a 2:1 split
    #: had been applied was the equity curve moving, which looks identical to a
    #: gain (AC-19). A reader comparing two runs — a split run against the same
    #: series already split by hand — needs the *position*, not just the money.
    #: Flat positions are omitted; a run that closed out reports ``[]``.
    end_positions: list[dict[str, Any]] = field(default_factory=list)
    #: The same executed fills as :attr:`fills`, but as the ``Fill`` objects
    #: the execution engine actually booked, with ``Decimal`` price and
    #: quantity intact. :attr:`fills` is a chart and report surface: its
    #: ``price`` and ``qty`` are ``float``, so a parity assertion built on it
    #: compares a rounded copy rather than what the book was debited. This is
    #: what ``round_trip_trades`` consumes.
    fill_records: list[Fill] = field(default_factory=list)
    # --- P1.4: richer, canonical metrics (all derived from the equity curve and
    # round trips; zero when the run has no trades). Additive with defaults, so
    # existing callers and the parity/golden tests are unaffected.
    #: Annualised downside-risk-adjusted return (Sortino).
    sortino: float = 0.0
    #: Annualised return divided by the worst peak-to-trough loss (Calmar).
    calmar: float = 0.0
    #: Annualised standard deviation of per-bar returns.
    volatility: float = 0.0
    #: Fraction of closed round trips that were profitable.
    win_rate: float = 0.0
    #: Gross profit over gross loss across closed round trips (0 when no loss).
    profit_factor: float = 0.0
    #: Average net P&L per closed round trip.
    expectancy: float = 0.0
    #: Fraction of the entry-to-exit span spent holding a position.
    exposure: float = 0.0


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
        initial_capital: Decimal | float | str = DEFAULT_INITIAL_CAPITAL,
        sharpe_frequency: str = "1m",
        stop_gap_bps: Decimal | float | str = DEFAULT_STOP_GAP_BPS,
        sizer: PositionSizer | None = None,
        max_order_value: Decimal | float | str = DEFAULT_MAX_ORDER_VALUE,
        risk_fraction: Decimal | float | str | None = DEFAULT_BACKTEST_RISK_FRACTION,
        engine: ExecutionEngine | None = None,
        partial_fills: bool = False,
        max_fill_fraction: Decimal | float | str = Decimal("0.2"),
        latency_bars: int = 0,
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
                accounting math as ``PositionAccountant`` (parity review area
                #4: corporate actions modeled consistently across modes).
            stop_gap_bps: Adverse fill beyond a triggered stop, in basis
                points. See :data:`DEFAULT_STOP_GAP_BPS` for the default and why
                it is not zero. ``0`` drops the slippage term and leaves the
                bar bound — which is arithmetic, not a choice, and can never
                beat the stop. A **negative** value would fill better than the
                stop and raises ``ValueError``.

        sizer:
            The ``PositionSizer`` that turns every signal in this run into an
            order size (AC-5). It is handed to **both** paths a strategy can
            emit on — the ``next_open`` queue and the recording-only bridge
            below — because two implementations of "signal -> quantity" is the
            parity break AC-5 exists to prevent, and the bridge's used to be
            ``abs(signal.strength)``. Leave it ``None`` and the run builds one
            from the two sizing arguments below.
        max_order_value, risk_fraction:
            The sizing policy for the sizer this run builds when none was
            handed in: a per-order notional ceiling (the same
            ``DEFAULT_MAX_ORDER_VALUE`` every unwired engine falls back to) and
            the fraction of *equity* one trade may lose. Equity is the run's
            **opening** capital, deliberately: sizing a research run against the
            balance its own P&L has produced is a feedback loop, so the same
            series would give a different answer for the same signals depending
            on how long the run was. Live sizing reads the account per signal
            instead (``equity_source``), which is the right behaviour for a
            session and the wrong one for a backtest.

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
        self._engine = engine
        self._initial_capital = Decimal(str(initial_capital))
        self._sharpe_frequency = sharpe_frequency
        gap_bps = Decimal(str(stop_gap_bps))
        if gap_bps < 0:
            # A negative gap is a request to fill a stop BETTER than its own
            # level — the exact optimism AC-19 removes, and unlike
            # ``fees=False`` it has no honest reading, so it is refused rather
            # than clamped. Zero stays legal: the bar bound still applies, so
            # the model can never beat the stop, which is what the negative
            # controls rely on.
            raise ValueError(
                f"stop_gap_bps must not be negative (got {stop_gap_bps!r}); a stop "
                "may not fill better than the price it was stopped at"
            )
        self._stop_gap_bps = gap_bps
        self._sizer = sizer
        self._max_order_value = Decimal(str(max_order_value))
        self._risk_fraction = None if risk_fraction is None else Decimal(str(risk_fraction))
        # P0.2: opt-in volume-capped partial fills. A MARKET order fills at most
        # ``max_fill_fraction`` of the reference bar's traded volume per submit;
        # the unfilled remainder is re-submitted at each following bar's open.
        self._partial_fills = partial_fills
        self._max_fill_fraction = Decimal(str(max_fill_fraction))
        if not (Decimal("0") < self._max_fill_fraction <= Decimal("1")):
            raise ValueError(
                "max_fill_fraction must be in (0, 1] (got "
                f"{max_fill_fraction!r})"
            )
        # P1.5: extra bars a next_open order waits before filling (order
        # latency). 0 keeps the next-bar fill; N re-prices at the N-th later
        # bar's open. Re-pricing is the engine's flush bar, so no separate
        # handling is needed here.
        if latency_bars < 0:
            raise ValueError(f"latency_bars must be >= 0, got {latency_bars!r}")
        self._latency_bars = latency_bars

    def _resolve_sizer(self) -> PositionSizer:
        """The sizer this run sizes every signal with (AC-5).

        The one it was given, or one built from the run's own policy. Built once
        per ``run`` and handed to *both* emission paths, so a signal that
        ``ReactiveStrategyEngine`` claims and a signal the recording-only bridge
        carries are sized by the same object — which is the whole of the parity
        claim. A signal the sizer cannot size is dropped with an ERROR and no
        order is published, on either path.
        """
        if self._sizer is not None:
            return self._sizer
        return PositionSizer(
            max_order_value=self._max_order_value,
            risk_fraction=self._risk_fraction,
            equity=self._initial_capital,
            fallback_notional=self._max_order_value,
            label="backtest",
        )

    def submit(self, request: Any) -> Any:
        """REMOVED — this was a second order door, and it skipped every gate.

        It called ``self._fill_source.submit(request)`` directly, which bypasses
        :meth:`~tradex_execution.engine.ExecutionEngine.submit` and with it the
        kill switch, the risk manager, the idempotency guard, the durable event
        log and the cash floor. A strategy wired to this method would have
        placed orders that the engine's book never recorded.

        It had **zero callers** — not in ``run()`` (which routes through
        ``engine.submit``), not in any package, not in any test — so removing it
        is a deletion, not a migration. The one door is
        :class:`~tradex_domain.protocols.OrderCommandPort`.
        """
        raise NotImplementedError(
            "BacktestEngine.submit was removed: it dispatched straight to the "
            "fill source, bypassing risk, idempotency, the kill switch and the "
            "cash floor. Route orders through ExecutionEngine.submit "
            "(tradex_domain.protocols.OrderCommandPort), which run() already "
            "does for both the next_open queue and the recording-only bridge."
        )

    def run(self, strategy: Strategy, data: list[Candle | Quote | Fill]) -> BacktestResult:
        """Run a backtest.

        Feed data events to the strategy, then compute real P&L from the
        emitted signals.

        **The fill rule, and it is the only one (AC-1).** A signal decided on
        bar N fills at bar N+1's *open* — whichever of the two paths carries
        it: the ``next_open`` queue in ``ReactiveStrategyEngine`` (a strategy
        that RETURNS its signal) or the recording-only bridge below (a
        strategy that only appends to ``strategy.signals``). The two paths used
        to disagree — the queue was flushed against the *last* candle of the
        run and the bridge filled at the *signal bar's own close* — so which
        path a signal took silently decided whether the strategy looked good.

        A signal whose next bar never arrives (the final bar of the run, or a
        quote after the last candle) has no price the strategy could have known
        when it decided, so it is NOT filled: it is counted in
        ``BacktestResult.num_unfilled`` and logged, and the run reports the
        count. Nothing is flushed against the last candle.

        **Cash is one ledger and one operation (AC-2).** The opening capital is
        handed to the ``ExecutionEngine`` (``cash=``), so its ``require_debit``
        proves affordability *before* a position is booked and then applies
        booking, notional and fee as a single sequence that cannot half-apply.
        Nothing on the bus moves money: the ``OrderFilled`` subscriber below
        only records. An overdraft therefore propagates out of this method
        instead of being absorbed by the bus's subscriber isolation, which is
        what made a too-big order produce a flattering equity curve with no
        error anywhere.

        **A protective exit does not fill at its own level (AC-19).** A triggered
        stop is a stop-*market*: it fills at the worse of the modelled gap past
        the trigger (``stop_gap_bps``) and the worst price the triggering bar
        printed — see :func:`_stop_gap_price`. A take-profit is a limit order
        and does not gap. Filling a stop exactly at its price is an optimism no
        venue grants, and it is invisible because the number it changes has the
        same shape as the number that was already right.

        **A corporate action is applied to the lot that held it (AC-19).** A
        split or dividend with an ex-date inside the window is applied at the
        first bar on or after it — so a split landing *between* two bars and one
        landing *on* a bar boundary take the same path. An action is applied
        only to lots that existed at its ex-date, and a split re-bases an open
        protective bracket's levels and size along with the position. Both
        halves matter: a re-based position next to an un-re-based stop exits at
        roughly twice the traded price, and a re-based lot that never held a
        pre-split share is a phantom gain. ``end_positions`` reports the
        resulting position so a run's corporate-action handling is visible
        rather than inferred from the equity curve.

        **Every signal is sized by the run's sizer (AC-5).** One
        ``PositionSizer`` — the one handed in, or one built from
        ``max_order_value`` / ``risk_fraction`` / ``initial_capital`` — sizes both
        the ``next_open`` queue and the recording-only bridge, so the two ways a
        strategy emits a signal cannot be sized by two different rules. A signal
        the sizer cannot size publishes nothing on either path and says why in
        the log; nothing here reads ``Signal.strength``, which is signal quality
        and was the order quantity.

        Args:
            strategy: Strategy instance
            data: List of events (Candle, Quote, Fill)
        Returns:
            BacktestResult with performance metrics

        Raises:
            ValueError: A fill the run's capital cannot pay for, or any other
                failure the order pipeline raised — re-raised out of the bus
                rather than swallowed, because every number this method returns
                is one an operator is about to trust.
        """
        initial_capital = self._initial_capital

        # Snapshot signals already on the strategy (e.g. a strategy object
        # reused from a prior ReplayEngine run) so this run only counts the
        # signals it emits itself — never pre-existing ones (parity area #5).
        _pre_signals = len(getattr(strategy, "signals", []) or [])

        # --- Unified reactive pipeline (same as live/replay/paper) -----------
        # Backtest is now a *bus driver*: it publishes historical events onto a
        # ReactiveBus; the strategy (via ReactiveStrategyEngine, next_open) sizes
        # its signal and hands the order to ExecutionEngine.submit() at the next
        # bar's open; the SAME ExecutionEngine + PositionAccountant + FeeCalculator
        # + RiskManager that live uses processes the order (AC-22: `submit()` is
        # the only door to the venue — the bus carries market data in and
        # observations out, never an order). Fills/risk/fees/positions are
        # therefore computed by one code path in every mode (parity review
        # CRITICAL-1). Backtest-specific reporting (cash ledger, point-in-time
        # MTM, sharpe/drawdown) is derived from PositionAccountant + CashLedger
        # state after the stream completes.
        bus = ReactiveBus()
        cache = TradingCache()
        position_manager = PositionAccountant(cache)
        # Collect candles per instrument_id up front: point-in-time MTM below and
        # the P0.2 volume cap both read it, and the fill source is built before
        # the stream is consumed.
        candles_by_id: dict[Any, list] = {}
        for event in data:
            if isinstance(event, Candle):
                candles_by_id.setdefault(event.instrument.instrument_id, []).append(event)
        # ``self._fill_source`` is deliberately ``Any`` (fill sources vary). The
        # original ``self._fill_source or SimulatedFillSource(...)`` was ``Any``
        # only because the left operand was; with a supplied source the value
        # is that source, and otherwise a fresh SimulatedFillSource. The engine
        # wants a ``FillSource``; both branches satisfy it at runtime, but mypy
        # cannot collapse the ``Any | SimulatedFillSource`` union on its own, so
        # the whole expression is narrowed once here. A bare annotation is not
        # enough (the ``Any`` member survives it). Runtime is unchanged — same
        # object, same construction, same pass-through.
        #
        # P0.2: with partial_fills on, the default source caps each fill at a
        # fraction of the reference bar's volume; the remainder chase below
        # re-submits the rest at each following bar's open.
        default_source = SimulatedFillSource(
            slippage_model=self._slippage_model,
            max_fill_fraction=(
                self._max_fill_fraction if self._partial_fills else Decimal("1")
            ),
            volume_provider=(
                _bar_volume_provider(candles_by_id) if self._partial_fills else None
            ),
        )
        fill_source = cast(FillSource, self._fill_source or default_source)
        engine = self._engine or ExecutionEngine(
            bus,
            fill_source,
            risk_manager=self._risk_manager,
            cache=cache,
            fee_calculator=self._fee_calculator,
            # THE cash ledger. Handing the engine the opening balance arms its
            # `require_debit` floor, which runs BEFORE a position is booked and
            # then applies booking + notional + fee as one non-failing
            # sequence (AC-2). Without `cash=` the engine holds no ledger at
            # all, the floor never runs, and an unaffordable order books a
            # position the run cannot pay for.
            #
            # The floor follows the risk manager: with one, an overdraft is a
            # run-ending error; without one, the historical no-gate mode keeps
            # its unlimited buying power.
            cash=initial_capital,
            allow_negative_cash=self._risk_manager is None,
        )

        # Brokerage is capped per ORDER, not per fill, and the engine already
        # keeps that accrual (`_brokerage_accrued`) against the same calculator,
        # so the backtest keeps no second copy of it.
        recorded_fills: list[dict[str, Any]] = []
        recorded_fill_objects: list[Fill] = []

        # Corporate-action cash effects (split basis restatement, dividend
        # credit) move the engine's ledger, which is both the affordability
        # floor and what the equity curve reports — so the gate reads the same
        # balance the run reports. `restore_cash` is the public setter; it
        # takes the pair because `cash_snapshot` is the only reader.
        def _apply_cash_delta(delta: Decimal) -> None:
            snapshot = engine.cash_snapshot()
            engine.restore_cash(snapshot.cash + Decimal(str(delta)), snapshot.total_fees)

        # Protective levels are a property of the ORDER, and a fill only carries
        # its order_id — so the legs are indexed as orders are placed and read
        # back when their fills arrive. `OrderPlaced` is published before the
        # `OrderFilled` of the same submit, so the entry is always there first.
        order_legs: dict[str, tuple[float | None, float | None, float | None]] = {}
        active_brackets: dict[Any, _OpenBracket] = {}
        protective_seq = 0
        remainder_seq = 0
        #: Bar each instrument's current lot was opened on, so a corporate
        #: action is applied only to the lots that existed at its ex-date.
        lot_opened_at: dict[Any, datetime] = {}
        #: P0.2 remainder chase: unfilled quantity left by a volume-capped
        #: fill, re-submitted at each following bar's open until filled.
        pending_remainder: list[dict[str, Any]] = []

        def _legs_of(
            order: Any,
        ) -> tuple[float | None, float | None, float | None]:
            stop = getattr(order, "stop_loss_price", None)
            target = getattr(order, "target_price", None)
            trailing = getattr(order, "trailing_jump", None)
            return (
                float(stop.value) if stop is not None else None,
                float(target.value) if target is not None else None,
                float(trailing.value) if trailing is not None else None,
            )

        def _on_placed(ev: OrderPlaced) -> None:
            order_legs[ev.order.order_id.value] = _legs_of(ev.order)

        bus.of_type(OrderPlaced).subscribe(_on_placed)

        def _register_bracket(
            f: Any,
            stop: float | None,
            target: float | None,
            trailing: float | None,
        ) -> None:
            entry = float(f.price.value)
            # A trailing-only protection carries no initial stop: seed one at
            # entry ∓ jump so the ratchet has a level to move from.
            if stop is None and trailing is not None:
                stop = (
                    entry - trailing
                    if f.side is OrderSide.BUY
                    else entry + trailing
                )
            if stop is None:
                return
            if target is not None:
                stop_p = Price(value=Decimal(str(stop)))
                target_p = Price(value=Decimal(str(target)))
                if not is_valid_bracket(f.side, f.price, stop_p, target_p):
                    return
            active_brackets[f.instrument.instrument_id] = _OpenBracket(
                instrument=f.instrument,
                side=f.side,
                quantity=f.quantity,
                stop=stop,
                target=target,
                trailing_jump=trailing,
                high_water=entry,
                low_water=entry,
            )

        def _rebase_bracket(inst_id: Any, ratio: Decimal) -> None:
            """Re-scale an open bracket's legs and size by a split/bonus ratio.

            A split re-bases the *position* (qty x ratio, avg / ratio) and the
            declared stop and target are prices in the same pre-split scale, so
            they must be re-based with it. Leaving them alone is worse than not
            applying the split at all: the position is 20 shares at 500.00 while
            the bracket still says *stop 950.00, sell 10*, so the next bar — in
            a market trading at 500.00 — "pierces" the stop and the run sells at
            950.00, a fill 90% above the market, on half the position (AC-19).

            The trailing distance and the high/low water marks are prices in the
            same pre-split scale, so they re-base by the same ratio; leaving the
            trail unscaled would make the stop jump on the next bar.
            """
            bracket = active_brackets.get(inst_id)
            if bracket is None or ratio <= 0:
                return
            r = float(ratio)
            active_brackets[inst_id] = _OpenBracket(
                instrument=bracket.instrument,
                side=bracket.side,
                quantity=Quantity(
                    value=bracket.quantity.value * ratio
                ),
                stop=bracket.stop / r,
                target=None if bracket.target is None else bracket.target / r,
                trailing_jump=(
                    None if bracket.trailing_jump is None
                    else bracket.trailing_jump / r
                ),
                high_water=bracket.high_water / r,
                low_water=bracket.low_water / r,
            )

        def _on_fill(ev: OrderFilled) -> None:
            f = ev.fill
            recorded_fill_objects.append(f)
            # No money moves here. The engine has already debited this fill's
            # notional and fee from the one ledger inside the same operation
            # that booked the position, after proving the debit was affordable;
            # a subscriber debit is what let an overdraft raise AFTER the
            # booking and be swallowed by the bus (AC-2). This handler records
            # the fill and maintains the bracket bookkeeping.
            stop, target, trailing = order_legs.get(
                f.order_id.value, (None, None, None)
            )
            recorded_fills.append({
                "time": f.timestamp,
                # Which instrument this record is about, stated by the record
                # rather than recovered later. `BacktestResult.fills` is the
                # input the validation report's round trip counter reads, and
                # positions are per instrument: a counter that has to guess
                # which symbol a fill belonged to either merges two symbols into
                # one ledger (inventing round trips — a buy in one and a sell in
                # the other reads as a position closed) or refuses outright
                # (which is what it did whenever two symbols shared a timestamp,
                # i.e. most multi-symbol research runs). Stated here it is exact
                # and unconditional. AC-13.
                "instrument": str(f.instrument.instrument_id),
                "side": str(getattr(f.side, "value", f.side)).upper(),
                "price": float(f.price.value),
                "qty": float(f.quantity.value),
                "rejected": False,
                "reason": "",
                # The levels the order carried, for a chart to draw.
                "stop": stop,
                "target": target,
                "trailing_jump": trailing,
            })
            inst_id = f.instrument.instrument_id
            if stop is not None or trailing is not None:
                _register_bracket(f, stop, target, trailing)
            else:
                bracket = active_brackets.get(inst_id)
                if bracket is not None and f.side is not bracket.side:
                    active_brackets.pop(inst_id, None)
            pos = cache.get_position(f.instrument)
            if pos is None or pos.quantity.value == Decimal("0"):
                active_brackets.pop(inst_id, None)
                lot_opened_at.pop(inst_id, None)
            elif inst_id not in lot_opened_at:
                # When the CURRENT lot opened. A corporate action is owed only
                # by the lots that existed at its ex-date; a position opened
                # afterwards never held a pre-split share, and re-basing it
                # invented a gain (AC-19). Cleared above when the lot goes flat,
                # so a later lot gets its own stamp.
                lot_opened_at[inst_id] = f.timestamp
            # P0.2: schedule the unfilled remainder of a volume-capped fill.
            if self._partial_fills:
                order = cache.get_order(f.order_id.value)
                if order is not None:
                    remaining = order.quantity.value - order.filled_quantity.value
                    if remaining > 0:
                        pending_remainder.append({
                            "instrument": f.instrument,
                            "side": f.side,
                            "remaining": remaining,
                        })

        bus.of_type(OrderFilled).subscribe(_on_fill)

        def _on_rejected(ev: OrderRejected) -> None:
            ts = getattr(ev.order, "reference_timestamp", None)
            stop, target, trailing = _legs_of(ev.order)
            recorded_fills.append({
                "time": ts or self._clock.now(),
                # The same key the filled record carries, and for the same
                # reason: a rejection is a record about one instrument, and a
                # shape that names it only on the happy path is empty exactly
                # when something needs explaining. A rejected record is not
                # counted as a fill, so this is for the audit trail and for
                # anything grouping records by symbol.
                "instrument": str(ev.order.instrument.instrument_id),
                "side": str(getattr(ev.order.side, "value", ev.order.side)).upper(),
                "price": 0.0,
                "qty": float(ev.order.quantity.value),
                "rejected": True,
                "reason": ev.reason,
                "stop": stop,
                "target": target,
                "trailing_jump": trailing,
            })

        bus.of_type(OrderRejected).subscribe(_on_rejected)

        # A failure inside the order pipeline is republished as ErrorOccurred
        # and then isolated by the bus like any other subscriber. Right for a
        # live session — one bad order must not kill the process — wrong for a
        # backtest, whose whole output is a number about to be trusted. Keep the
        # first one and re-raise it out of `run()`.
        pipeline_errors: list[BaseException] = []

        def _on_pipeline_error(ev: ErrorOccurred) -> None:
            if not pipeline_errors:
                pipeline_errors.append(ev.error)

        bus.of_type(ErrorOccurred).subscribe(_on_pipeline_error)

        # Strategy engine (next_open) drives orders at next-open. The SAME
        # sizer the recording-only bridge below asks, built once for this
        # run — AC-5's parity half. Left to itself the engine falls back to
        # `unconfigured_sizer()` and the bridge to its own `abs(strength)`, so
        # the two ways a strategy emits would be sized by two different rules.
        #
        # `submit` is the engine's own `submit` (AC-22): the strategy hands its
        # sized, bracketed request straight to the pipeline instead of
        # publishing a command onto the bus for someone to pick up. The bus
        # stays a driver of market data into the run and a source of
        # observations out of it.
        sizer = self._resolve_sizer()
        # Fresh runtime per run: no cross-run leakage of bars or cached
        # indicator state. The engine ingests each candle into it and hands
        # the resulting view to the strategy via StrategyContext.analytics.
        from tradex_analytics.runtime import AnalyticsRuntime
        analytics = AnalyticsRuntime()
        strategy_engine = ReactiveStrategyEngine(
            bus,
            engine.submit,
            fill_reference="next_open",
            sizer=sizer,
            analytics=analytics,
            fill_delay_bars=self._latency_bars,
            # Read-only context views (Phase 6-7): the same cache and ledger
            # the risk manager reads, so a strategy's view of its position
            # and balance is the run's own book — no second source.
            positions_provider=lambda: cache.all_positions(),
            balance_provider=lambda: Money(amount=engine.cash_snapshot().cash),
        )
        strategy_engine.register(strategy)

        # Bind the OMS cache and the run clock BEFORE any order is submitted,
        # so the risk manager's rate window is anchored to backtest time, not
        # wall time (parity review area #8 / Task 4 clock fix).
        if self._risk_manager is not None:
            if not getattr(self._risk_manager, "positions_provider_bound", False):
                self._risk_manager.set_positions_provider(
                    lambda: list(cache.all_positions()),
                )
            if not getattr(self._risk_manager, "cash_provider_bound", False):
                self._risk_manager.bind_cash_provider(
                    lambda: engine.cash_snapshot().cash,
                )
            self._risk_manager.set_clock(self._clock)
            self._risk_manager.reset_rate_window()

        # Corp actions: applied to the shared PositionAccountant (and the ledger's
        # cash effects mirrored) at each bar's ex-date — before that bar's fill.
        applied_actions: dict[str, set[str]] = {}
        def _apply_ca_for(ts: datetime) -> None:
            if self._corporate_actions is None:
                return
            for pos in list(cache.all_positions()):
                self._apply_actions_due(
                    pos.instrument, position_manager, _apply_cash_delta,
                    ts, applied_actions, lot_opened_at, _rebase_bracket,
                )


        # Stamp the strategy version onto each signal's metadata (audit trail).
        # The strategy is fed events through the bus; signals are collected
        # from the strategy object after the stream (parity area #5).
        version = str(getattr(strategy, "version", "1.0.0"))

        # --- Recorded-signal bridge (same fill rule as the next_open engine) --
        # A strategy that RECORDS signals (appends to ``strategy.signals``)
        # instead of returning them from its callbacks never reaches the
        # strategy engine — the engine only bridges signals that ``on_bar`` /
        # ``on_quote`` RETURN. This bridge carries those, and it obeys the SAME
        # rule the engine does: a signal is deferred to the next Candle of its
        # instrument and filled at that candle's OPEN. It used to fill at the
        # signal bar's own close, which is a price the strategy could not have
        # known when it decided (AC-1).
        #
        # Which signals the engine has claimed (returned, and queued for
        # next_open) is tracked by identity, so a signal is bridged exactly once
        # whichever path carries it: a strategy that returns its signal keeps
        # its order flowing through the engine, a strategy that records it gets
        # the same fill at the same price, and a strategy that does both gets
        # one order, not two.
        claimed_signal_ids: set[int] = set()
        # Signals this bridge has taken — queued for the next bar or already
        # published. A signal recorded on bar N is queued *after* bar N is
        # processed, so the earliest bar it can fill against is N+1.
        bridged_signal_ids: set[int] = set()
        # Deferred bridge signals, keyed by instrument, in arrival order.
        bridge_pending: dict[Any, list[tuple[int, Signal]]] = {}
        #: Candle index, so a bridge signal can be released ``latency_bars``
        #: after the bar it was recorded on (P1.5).
        bar_index = 0
        bridge_seq = 0

        def _capture_claimed() -> None:
            for pending in strategy_engine.pending_snapshot():
                claimed_signal_ids.add(id(pending["signal"]))

        def _capture_recorded(inst_id: Any) -> None:
            """Queue the signals this strategy recorded, for *inst_id*'s next
            candle. Called after each event, so a signal recorded while bar or
            quote N was processed can only fill from bar N+1 onwards.

            Skipped entirely once the engine has claimed a signal: a strategy
            that RETURNS its signals owns its own order flow, and its
            ``strategy.signals`` list is the full history including signals it
            has not returned yet. Bridging those would double-fill one on the
            engine's next-open queue. Callers must pick one way to emit — this
            bridge is for recording-only strategies.
            """
            if claimed_signal_ids:
                return
            for signal in getattr(strategy, "signals", []) or []:
                if id(signal) in bridged_signal_ids or id(signal) in claimed_signal_ids:
                    continue
                if signal.instrument.instrument_id != inst_id:
                    continue
                bridged_signal_ids.add(id(signal))
                bridge_pending.setdefault(inst_id, []).append((bar_index, signal))

        def _maybe_protective_exit(candle: Candle) -> None:
            nonlocal protective_seq
            inst_id = candle.instrument.instrument_id
            bracket = active_brackets.get(inst_id)
            if bracket is None:
                return
            pos = cache.get_position(candle.instrument)
            if pos is None or pos.quantity.value == Decimal("0"):
                active_brackets.pop(inst_id, None)
                return
            high = float(candle.ohlc.high.value)
            low = float(candle.ohlc.low.value)
            # Test the trigger against the stop as it stood at the bar's open
            # (i.e. ratcheted by *prior* bars), THEN ratchet the trailing stop
            # using this bar's extreme for subsequent bars. Ratcheting first
            # would stop a long out on the very bar that made the new high,
            # because the same bar's low is compared against a stop derived
            # from that same bar's high — an intrabar impossibility.
            kind, level = _protective_trigger(
                candle, bracket.side, bracket.stop, bracket.target,
            )
            if level is None or kind is None:
                # Not triggered: advance the trailing stop for the next bar.
                if bracket.trailing_jump is not None:
                    jump = bracket.trailing_jump
                    if bracket.side is OrderSide.BUY:
                        bracket.high_water = max(bracket.high_water, high)
                        bracket.stop = max(
                            bracket.stop, bracket.high_water - jump
                        )
                    else:
                        bracket.low_water = min(bracket.low_water, low)
                        bracket.stop = min(
                            bracket.stop, bracket.low_water + jump
                        )
                return
            # A triggered stop does not fill at its own level: it fills at the
            # worse of the modelled slippage past the trigger and the worst
            # price the bar actually printed (AC-19). A take-profit is a limit
            # order and is left alone — see `_stop_gap_price`.
            exit_price = _stop_gap_price(
                kind, bracket.side, level, low, high, self._stop_gap_bps,
            )
            active_brackets.pop(inst_id, None)
            protective_seq += 1
            exit_side = (
                OrderSide.SELL if bracket.side is OrderSide.BUY else OrderSide.BUY
            )
            request = OrderRequest(
                instrument=bracket.instrument,
                side=exit_side,
                order_type=OrderType.MARKET,
                quantity=bracket.quantity,
                price=Price(value=Decimal(str(exit_price))),
                correlation_id=CorrelationId(
                    value=mint_correlation_id("backtest-protect", protective_seq),
                ),
                tag="backtest-protective-exit",
                reference_timestamp=candle.timestamp,
            )
            # Straight to the engine (AC-22) — the bus is not an order door.
            engine.submit(request)

        def _bridge_signal(signal: Signal, candle: Candle) -> None:
            """Publish *signal*'s order against *candle* — the bar AFTER the one
            it was decided on, priced at that bar's open."""
            nonlocal bridge_seq
            bridge_seq += 1
            open_price = Price(value=Decimal(str(candle.ohlc.open.value)))
            # The size is the sizer's, and the price it is sized against is the
            # one the order will actually carry — the next bar's open, not the
            # signal bar's close (AC-1). It used to be
            # `abs(signal.strength) if signal.strength else 1.0`, which sized
            # every shipped strategy (all of which emit strength=1.0) to one
            # share and disagreed with the engine path on every signal that
            # carried a number. A signal the sizer cannot size publishes
            # nothing, exactly as on the engine path.
            decision = sizer.size(signal, reference_price=open_price)
            if decision.quantity is None:
                log.error(
                    "backtest: dropped %s signal %r for %s — %s",
                    getattr(strategy, "strategy_id", "strategy"),
                    getattr(signal, "reason", ""), signal.instrument.instrument_id,
                    decision.reason,
                )
                return
            # Same declaration as the engine-driven paths: a recording-only
            # strategy's levels must reach the order too, or the two ways a
            # strategy can emit a signal would carry different protection.
            request = protective_request(
                signal=signal,
                entry=open_price,
                quantity=decision.quantity,
                correlation_id=CorrelationId(
                    value=mint_correlation_id("backtest-bridge", bridge_seq)
                ),
                tag=f"{getattr(strategy, 'strategy_id', 'strategy')}@{version}",
                reference_timestamp=candle.timestamp,
            )
            engine.submit(request)

        def _flush_bridge_pending(candle: Candle) -> None:
            """Fill the next bridge-deferred signal for *candle*'s instrument at
            its open, before the strategy sees the bar.

            Runs at the same point in the bar as the engine's own
            ``_flush_pending``, so a recorded signal and a returned one are
            filled at the same price from the same information. ONE signal per
            bar is the throttle this bridge has always applied ("signal N
            consumes candle N"), kept so a strategy holding several unmatched
            recorded signals still trades them as a ladder across bars instead
            of collapsing onto one bar's open.
            """
            queued = bridge_pending.get(candle.instrument.instrument_id)
            if not queued:
                return
            queued_at, signal = queued[0]
            # Not yet due: a recorded signal fills one bar after it was
            # recorded, plus any configured latency (P1.5).
            if bar_index < queued_at + 1 + self._latency_bars:
                return
            queued.pop(0)
            _bridge_signal(signal, candle)

        def _flush_remainder(candle: Candle) -> None:
            """Re-submit one instrument's unfilled remainder at *candle*'s open.

            P0.2 only. A volume-capped fill leaves a remainder; this drives it
            at each following bar's open until it fills or the data runs out. If
            the bar has no liquidity (no fill at all), the remainder is kept for
            the next bar rather than dropped.
            """
            nonlocal remainder_seq
            if not pending_remainder:
                return
            inst_id = candle.instrument.instrument_id
            idx = next(
                (
                    i for i, rec in enumerate(pending_remainder)
                    if rec["instrument"].instrument_id == inst_id
                ),
                None,
            )
            if idx is None:
                return
            rec = pending_remainder.pop(idx)
            qty = rec["remaining"]
            if qty <= 0:
                return
            open_price = Price(value=Decimal(str(candle.ohlc.open.value)))
            remainder_seq += 1
            request = OrderRequest(
                instrument=rec["instrument"],
                side=rec["side"],
                order_type=OrderType.MARKET,
                quantity=Quantity(value=qty),
                price=open_price,
                correlation_id=CorrelationId(
                    value=mint_correlation_id("backtest-remainder", remainder_seq)
                ),
                tag="backtest-remainder",
                reference_timestamp=candle.timestamp,
            )
            receipt = engine.submit(request)
            # No fill (zero liquidity this bar): keep chasing on the next bar.
            # A partial fill re-queues itself through ``_on_fill`` instead.
            new_order = cache.get_order(receipt.order_id.value)
            if new_order is not None and new_order.filled_quantity.value <= 0:
                pending_remainder.append(rec)

        # Stream historical events through the unified pipeline.
        equity_curve_dec: list[Decimal] = [initial_capital]
        for event in data:
            if pipeline_errors:
                # A pipeline failure has already ended this run. Grinding the
                # rest of the window would only produce more numbers from a
                # book that is provably wrong; the run is discarded below.
                break
            if isinstance(event, Candle):
                # Apply due corporate actions BEFORE the bar's open fill.
                self._clock.set(event.timestamp)
                bar_index += 1
                _apply_ca_for(event.timestamp)
                _flush_bridge_pending(event)
                _flush_remainder(event)
                bus.publish(event)
                _capture_claimed()
                _capture_recorded(event.instrument.instrument_id)
                _maybe_protective_exit(event)
                # Point-in-time MTM at this bar's close.
                positions_now = {
                    p.instrument.instrument_id: p for p in cache.all_positions()
                }
                equity_curve_dec.append(
                    engine.cash_snapshot().cash
                    + self._mark_to_market(positions_now, candles_by_id, event.timestamp)
                )
            elif isinstance(event, Quote):
                self._clock.set(event.timestamp)
                bus.publish(event)
                _capture_claimed()
                _capture_recorded(event.instrument.instrument_id)
            elif isinstance(event, Fill):
                # A recorded fill is a bare `Fill`; the pipeline listens for the
                # `OrderFilled` EVENT. Publishing the raw object meant a
                # recorded tape booked zero fills and zero fees — the backtest
                # silently could not reproduce a real session.
                bus.publish(OrderFilled(fill=event))
                _capture_claimed()

        # --- Signals with no bar left to fill against -----------------------
        # Both paths defer to the next bar of their instrument, so anything
        # still queued when the data runs out has no price the strategy could
        # have known when it decided. It is reported, never filled against the
        # last candle (AC-1) — that flush is exactly the look-ahead it replaced.
        unfilled = len(strategy_engine.pending_snapshot()) + sum(
            len(queued) for queued in bridge_pending.values()
        )
        if unfilled and not pipeline_errors:
            log.warning(
                "backtest left %d signal(s) unfilled: the run ended before the "
                "bar they must fill against arrived. They are excluded from "
                "num_trades and the equity curve.",
                unfilled,
            )

        engine.shutdown()
        strategy_engine.dispose_all()

        if pipeline_errors:
            failure = pipeline_errors[0]
            log.error(
                "backtest pipeline failed and the run is discarded: %s: %s",
                type(failure).__name__, failure,
            )
            raise failure

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
        total_fees = engine.cash_snapshot().total_fees

        # The position book the run ended on. A corporate action is otherwise
        # invisible in a run's output: a split used to show up only as the
        # equity curve moving, which is indistinguishable from a gain (AC-19).
        end_positions = [
            {
                "instrument_id": str(p.instrument.instrument_id),
                "symbol": p.instrument.symbol,
                "quantity": float(p.quantity.value),
                "avg_price": float(p.avg_price.value),
                "realized_pnl": float(p.realized_pnl.amount),
            }
            for p in sorted(
                cache.all_positions(), key=lambda p: str(p.instrument.instrument_id)
            )
            if p.quantity.value != Decimal("0")
        ]

        # Build float equity curve and returns.
        equity_curve = [float(v) for v in equity_curve_dec]
        returns: list[float] = []
        for i in range(1, len(equity_curve)):
            prev = equity_curve[i - 1]
            returns.append((equity_curve[i] - prev) / prev if prev != 0.0 else 0.0)

        # C3: infer Sharpe frequency from candle timeframe when default "1m" is left on D1 data
        # ponytail: silent fallback when no timeframe — raise when strict mode needed
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
                    else:
                        log.warning("sharpe_frequency fallback to '1m': no timeframe on data[0]")
                else:
                    log.warning("sharpe_frequency fallback to '1m': no timeframe on data[0]")
            except Exception:
                pass

        # --- P1.4: richer metrics from the same canonical definitions --------
        # Per-trade stats come from round trips (one row per closed position),
        # never from ``num_trades`` (which counts fills). Equity-curve metrics
        # annualise with the same frequency the Sharpe already uses.
        periods = periods_per_year(frequency)
        if returns:
            mean_r = sum(returns) / len(returns)
            if len(returns) > 1:
                var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
                volatility = (var_r ** 0.5) * (periods ** 0.5)
            else:
                volatility = 0.0
            annualised_return = mean_r * periods
        else:
            volatility = 0.0
            annualised_return = 0.0
        run_max_dd = max_drawdown(equity_curve)
        calmar = (
            annualised_return / abs(run_max_dd) if run_max_dd != 0.0 else 0.0
        )
        stats = compute_statistics(
            round_trips_from_fills(recorded_fills),
            equity_curve=equity_curve,
            frequency=frequency,
        )

        return BacktestResult(
            total_return=total_return(equity_curve),
            sharpe=sharpe_ratio(returns, frequency=frequency),
            max_drawdown=run_max_dd,
            num_trades=num_trades,
            trades=signals,
            total_fees=float(total_fees),
            equity_curve=equity_curve,
            num_rejected=rejected,
            num_unfilled=unfilled,
            fills=sorted(recorded_fills, key=lambda f: f["time"]),
            # Same order as `fills`, same objects, no float round trip.
            fill_records=sorted(recorded_fill_objects, key=lambda f: f.timestamp),
            end_positions=end_positions,
            sortino=float(stats.sortino),
            calmar=calmar,
            volatility=volatility,
            win_rate=stats.win_rate,
            profit_factor=stats.profit_factor,
            expectancy=float(stats.expectancy),
            exposure=stats.exposure,
        )

    def _apply_actions_due(
        self,
        instrument: Any,
        position_manager: Any,
        apply_cash_delta: Callable[[Decimal], None],
        as_of: datetime | None,
        applied: dict[str, set[str]],
        lot_opened_at: dict[Any, datetime] | None = None,
        rebase_bracket: Callable[[Any, Decimal], None] | None = None,
    ) -> None:
        """Apply corporate actions with ex-date <= *as_of* to the shared
        PositionAccountant (and mirror their cash effects onto the ledger).

        Point-in-time: only actions already effective by *as_of* are applied,
        in ex-date order, and each action exactly once per run. The position
        math is delegated to ``PositionAccountant.on_corporate_action`` — the same
        ``apply_split`` / ``apply_dividend`` model the reactive pipeline uses
        (parity review area #4), so backtest, replay, paper, and live book
        corporate actions identically. *apply_cash_delta* is called with the
        same basis restatement (split) and per-share credit (dividend) the
        legacy private loop applied, so realized P&L and equity stay equal to
        the rupee. It writes to the engine's one ledger, which is also the
        affordability floor — an action that only moved a reporting copy would
        leave the gate reading a balance the run does not report.

        **Point-in-time has a second clause: the lot must have existed at the
        ex-date.** ``ex_date <= as_of`` says the action is *effective*; it does
        not say this position *held* the shares it re-bases. A run that entered
        after the ex-date and re-based anyway doubled a lot that never held a
        pre-split share, and then sold half of it at the pre-split price — a
        phantom gain, and the same class of look-ahead AC-19 exists to remove.
        *lot_opened_at* carries the bar each instrument's current lot opened on;
        an action whose ex-date predates it is skipped and burned (it can never
        be owed on that lot, and a lot opened later is owed nothing either).

        *rebase_bracket* re-scales an open protective pair by a split/bonus
        ratio. Without it, applying the split leaves the stop and target in the
        pre-split scale while the position is already re-based, so the next bar
        "pierces" a stop that is now far above the market and the run exits at
        roughly twice the traded price (AC-19).
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
        inst_id = instrument.instrument_id
        opened_on = (lot_opened_at or {}).get(inst_id)
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
            if opened_on is not None and opened_on.date() > _parse_ex_date(action.ex_date):
                continue  # this lot was opened after the ex-date — not owed
            kind = action.action_type.upper()
            if kind in ("SPLIT", "BONUS") and action.ratio > 0:
                old_basis = existing.avg_price.value * existing.quantity.value
                position_manager.on_corporate_action(
                    instrument, kind, ratio=action.ratio,
                )
                existing = position_manager._cache.get_position(instrument)
                new_basis = existing.avg_price.value * existing.quantity.value
                apply_cash_delta(old_basis - new_basis)
                if rebase_bracket is not None:
                    rebase_bracket(inst_id, Decimal(str(action.ratio)))
            elif kind == "DIVIDEND":
                per_share = Decimal(str(action.amount))
                apply_cash_delta(q2(per_share * existing.quantity.value))
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
            quote = Quote(
                instrument=pos.instrument,
                ltp=Price(value=last_close),
                timestamp=as_of or inst_candles[-1].timestamp,
            )
            selected = MarkToMarketService.select_mark(pos, quote)
            mark = selected[0].value if selected is not None else last_close
            mtm += qty * mark
        return mtm

