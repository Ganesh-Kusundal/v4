"""Risk gate — extracted from engine.py (PE-6).

Provides the RiskManager with configurable limits: order value, position
value, rate limiting, daily-loss, drawdown, per-strategy budgets, cash
gate, and fresh-mark live safety.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from tradex_domain.enums import OrderSide
from tradex_domain.execution import OrderRequest
from tradex_domain.protocols import Clock
from tradex_domain.risk_budget import RiskBudget, resolve_strategy_id

if TYPE_CHECKING:
    from tradex_domain.execution import Position
    from tradex_domain.market import Quote
    from tradex_observability.metrics import MetricsRegistry

log = logging.getLogger(__name__)

__all__ = [
    "RiskBudget",
    "RiskCheckResult",
    "RiskManager",
]


@dataclass(frozen=True, slots=True)
class RiskCheckResult:
    """Outcome of a risk check — approved flag plus human-readable reason."""

    approved: bool
    reason: str = ""


class RiskManager:
    """Simple risk manager with configurable limits."""

    def __init__(
        self,
        max_order_value: Decimal | None = None,
        max_position_value: Decimal | None = None,
        max_orders_per_minute: int | None = None,
        *,
        live_orders_enabled: bool = True,
        #: Short-selling policy. When False, a SELL that would open or
        #: grow a short position is denied; a SELL that only trims or
        #: closes a long stays allowed (flattening is never blocked).
        #: Class-level default True preserves every existing direct
        #: constructor; ``boot()`` binds ``RiskConfig.allow_short``
        #: (default False) so the platform boundary is safe by default.
        allow_short: bool = True,
        positions_provider: Callable[[], list[Position]] | None = None,
        price_provider: Callable[[Any], Quote | None] | None = None,
        reject_unknown_market_value: bool = False,
        require_fresh_marks: bool = False,
        max_mark_age_seconds: float = 5.0,
        max_daily_loss_amt: Decimal | None = None,
        max_drawdown_pct: Decimal | None = None,
        #: Optional cap on total gross exposure (sum of ``abs(qty) * mark``
        #: across the book, measured after the incoming order). ``None``
        #: (default) means no cap — backward compatible.
        max_gross_exposure: Decimal | None = None,
        #: Optional cap on absolute net exposure (signed ``qty * mark``
        #: summed across the book, after the incoming order). ``None``
        #: (default) means no cap — backward compatible.
        max_net_exposure: Decimal | None = None,
        budgets: dict[str, RiskBudget] | None = None,
        clock: Clock | None = None,
        metrics: MetricsRegistry | None = None,
        baseline_store: object | None = None,
    ) -> None:
        self._max_order_value = max_order_value
        self._max_position_value = max_position_value
        self._max_orders_per_minute = max_orders_per_minute
        self._recent_orders: deque[datetime] = deque()
        self._lock = threading.Lock()
        self._live_orders_enabled = live_orders_enabled
        self._allow_short = allow_short
        #: When True, a MARKET order whose price/mark cannot be resolved is
        #: denied (fail-closed for live); when False it preserves the legacy
        #: behaviour of skipping the order-value gate (dev/paper).
        self._reject_unknown_market_value = reject_unknown_market_value
        #: Live safety gate: opening/increasing exposure requires fresh
        #: quote-derived marks for the cached position book.
        self._require_fresh_marks = require_fresh_marks
        self._max_mark_age_seconds = float(max_mark_age_seconds)
        if self._max_mark_age_seconds < 0:
            raise ValueError("max_mark_age_seconds must be non-negative")
        #: Daily-loss and drawdown limits. Guards only get tighter as the day
        #: progresses; they always allow reductions/flattening.
        self._max_daily_loss_amt = max_daily_loss_amt
        self._max_drawdown_pct = max_drawdown_pct
        self._max_gross_exposure = max_gross_exposure
        self._max_net_exposure = max_net_exposure
        #: M5: real type (was ``Any``, defeating mypy). The fields store
        #: the calendar date the account-level baseline was taken, used to roll
        #: the day-start P&L forward on a date change.
        self._session_date: date | None = None
        self._base_pnl = Decimal(0)
        self._peak_pnl = Decimal(0)
        #: AC-9: per-strategy day-start baseline and running peak, keyed by the
        #: resolved strategy id. These are what the daily-loss/drawdown guards
        #: evaluate, so one strategy's loss cannot be attributed to another and
        #: a restart can read them back. ``_session_date``/``_base_pnl``/
        #: ``_peak_pnl`` above remain the account-level aggregate used for the
        #: gauges.
        self._strategy_baselines: dict[str, tuple[date, Decimal, Decimal]] = {}
        #: Callable returning current positions (e.g. an OMS cache) so
        #: ``max_position_value`` can be enforced against live exposure. When
        #: None (backtest boot, unit tests), the position check is skipped.
        self._positions_provider = positions_provider
        #: Callable mapping an instrument to its current market price (Price
        #: or Decimal). When None (or when it yields no usable price),
        #: exposure falls back to the position's avg_price.
        self._price_provider = price_provider
        #: AC-9: per-strategy risk budgets. ``check()`` resolves the active
        #: budget from the request's strategy identifier (the real
        #: ``strategy_id`` field, else the ``strategy_id@version`` text in
        #: ``tag``). A request with no matching budget falls back to the
        #: global caps above.
        self._budgets: dict[str, RiskBudget] = dict(budgets or {})
        self._clock = clock
        #: Optional durable store for the per-strategy baselines (a
        #: ``SQLiteRiskBaselineStore``). When bound, baselines are loaded at
        #: construction and saved as they move, so a crash-restart does not
        #: reset the breaker. Typed as ``object`` to keep this module free of
        #: an import cycle with the store; the duck-typed calls below are the
        #: store's small public surface. Loaded here, after ``_clock`` is set,
        #: because reading a stored baseline needs the clock seam for today's date.
        self._baseline_store = baseline_store
        if self._baseline_store is not None:
            self._load_baselines()
        #: Callable returning the current cash balance (Decimal). Bound
        #: lazily via ``bind_cash_provider()`` so the engine can boot
        #: without a cash ledger (backtest, unit tests). Initialized to
        #: None explicitly (M9 fix — previously relied on getattr fallback).
        self._cash_provider: Callable[[], Decimal | None] | None = None
        #: When True, a BUY is denied unless a cash provider is bound AND
        #: returns a balance. Live and paper set this so an unavailable
        #: balance stops admission instead of silently skipping the gate —
        #: a recovered or unreadable balance must never read as "sufficient".
        self.fail_closed_cash: bool = False
        #: Count of orders denied by ``check()`` (any gate). Read by
        #: BacktestEngine to populate ``BacktestResult.num_rejected`` without
        #: re-implementing rejection bookkeeping in its own loop.
        self._rejected_count = 0
        #: C3: optional metrics registry for per-reason rejection counters
        #: and exposure/drawdown/daily-loss gauges.
        self._metrics: MetricsRegistry | None = metrics
        #: C3: last deny reason string (set under _lock in _deny()).
        #: ponytail: TOCTOU race if check_order() called concurrently; check()
        #: (the production path in the engine) is not affected.
        self._last_deny_reason: str = ""

    def _active_budget(self, request: OrderRequest) -> tuple[Decimal | None, ...]:
        """Resolve the active cap values for ``request``.

        Returns ``(max_order_value, max_position_value,
        max_daily_loss_amt, max_drawdown_pct)`` for the strategy, or
        the global caps if the strategy has no budget. AC-9.

        The strategy is identified by :func:`resolve_strategy_id`, which
        prefers the real ``request.strategy_id`` field and falls back to
        parsing the text before ``@`` out of ``request.tag``. The old code
        compared the *whole* tag (``"sma_cross@1.0.0"``) against a budget key
        (``"sma_cross"``), so the lookup could never match and every request
        silently fell through to the global caps — the documented per-strategy
        control was dead. A request with no attributable strategy still falls
        back to the global caps.
        """
        sid = resolve_strategy_id(request) if request is not None else None
        budget = self._budgets.get(sid) if sid else None
        if budget is not None:
            return (
                budget.max_order_value,
                budget.max_position_value,
                budget.max_daily_loss_amt,
                budget.max_drawdown_pct,
            )
        return (
            self._max_order_value,
            self._max_position_value,
            self._max_daily_loss_amt,
            self._max_drawdown_pct,
        )

    def _position_exposure(self) -> Decimal:
        """Absolute notional of all open positions (qty * market or avg price).

        Prefers the current market price via the bound ``price_provider``
        when available; falls back to the position's avg_price otherwise.
        """
        total = Decimal(0)
        if self._positions_provider is None:
            return total
        positions = self._positions_provider()
        for pos in positions:
            qty = getattr(pos, "quantity", None)
            avg = getattr(pos, "avg_price", None)
            if qty is None or avg is None:
                continue
            mark = self._mark_price(getattr(pos, "instrument", None))
            total += abs(qty.value) * (mark if mark is not None else avg.value)
        return total

    def _resulting_exposures(self, request: OrderRequest) -> tuple[Decimal, Decimal]:
        """Gross and net notional of the book as it would stand after *request*.

        Marks every position exactly as :meth:`_position_exposure` does
        (market mark when available, else avg_price) and adds the order's
        signed quantity to the matching position — the same per-side
        arithmetic as :meth:`_increases_exposure`. With no matching
        position (or no book at all) the order's own notional is added:
        it opens a new position.
        """
        signed = (
            request.quantity.value
            if request.side is OrderSide.BUY
            else -request.quantity.value
        )
        gross = Decimal(0)
        net = Decimal(0)
        matched = False
        if self._positions_provider is not None:
            for pos in self._positions_provider():
                qty = getattr(pos, "quantity", None)
                avg = getattr(pos, "avg_price", None)
                if qty is None or avg is None:
                    continue
                resulting = qty.value
                if pos.instrument == request.instrument:
                    resulting += signed
                    matched = True
                mark = self._mark_price(getattr(pos, "instrument", None))
                price = mark if mark is not None else avg.value
                gross += abs(resulting) * price
                net += resulting * price
        if not matched:
            mark = self._mark_for_trade(request)
            if mark is not None:
                gross += mark * request.quantity.value
                net += signed * mark
        return gross, net

    def _mark_price(self, instrument: Any) -> Decimal | None:
        """Current market price for an instrument, or None if unavailable."""
        if self._price_provider is None or instrument is None:
            return None
        try:
            quote = self._price_provider(instrument)
        except Exception:
            return None
        if quote is None:
            return None
        if hasattr(quote, "ltp"):
            value = getattr(quote.ltp, "value", quote.ltp)
        else:
            value = getattr(quote, "value", quote)
        try:
            value = Decimal(str(value))
        except Exception:
            return None
        return value if value > 0 else None

    def _mark_for_trade(self, request: OrderRequest) -> Decimal | None:
        """Effective price for notional checks: the request price, else the
        current market mark. ``None`` when neither is resolvable.

        MARKET orders often carry no price; without this, their notional
        collapses to zero and the order-value gate is silently bypassed.
        """
        price = request.price
        if request.side is OrderSide.SELL and self._price_provider is not None:
            try:
                quote = self._price_provider(request.instrument)
                bid = getattr(quote, "bid", None) if quote is not None else None
                bid = getattr(bid, "value", bid)
                bid = Decimal(str(bid)) if bid is not None else None
                if bid is not None and bid > 0:
                    return max(price.value, bid) if price is not None and price.value > 0 else bid
            except Exception:
                pass
        if price is not None and price.value > 0:
            return price.value
        return self._mark_price(request.instrument)

    def _incoming_exposure(self, request: OrderRequest) -> Decimal:
        """Notional of the incoming order (price * quantity), or 0 if unpriced."""
        mark = self._mark_for_trade(request)
        if mark is None:
            return Decimal(0)
        return mark * request.quantity.value

    def _unpriced_cash_check(self, request: OrderRequest) -> bool:
        """True when the cash gate would compare a missing price against cash.

        ``_incoming_exposure`` reports zero for an order it cannot value. Zero
        then compares as "affordable", which admits an order whose cost was
        never computed — a BUY closing a short with no quote is the case that
        reaches here, because reducing exposure skips the fresh-mark gate. Under
        ``fail_closed_cash`` a missing number is a stop, not a pass.
        """
        if not self.fail_closed_cash:
            return False
        if self._mark_for_trade(request) is not None:
            return False
        return request.side is OrderSide.BUY and bool(request.quantity.value)

    @property
    def live_orders_enabled(self) -> bool:
        """Master gate: when False, all orders are rejected."""
        return self._live_orders_enabled

    @live_orders_enabled.setter
    def live_orders_enabled(self, value: bool) -> None:
        self._live_orders_enabled = value

    def set_positions_provider(self, provider: Callable[[], list[Position]]) -> None:
        """Bind the position source used for ``max_position_value``.

        ``provider`` is a zero-arg callable returning an iterable of
        positions (e.g. ``engine.cache.all_positions``). When unset the
        position check is skipped.
        """
        self._positions_provider = provider

    def set_price_provider(self, provider: Callable[[Any], Quote | None]) -> None:
        """Bind a market-price source for exposure marking.

        ``provider`` is a one-arg callable mapping an instrument to its
        current price (``Price`` or ``Decimal``). When unset — or when it
        returns nothing usable — exposure falls back to avg_price.
        """
        self._price_provider = provider

    def set_clock(self, clock: Clock) -> None:
        """Bind a deterministic clock for rate-limit window evaluation.

        Required for reproducible backtests: without an explicit clock or an
        explicit ``now=`` on each ``check()``, the manager raises rather than
        sampling wall time.
        """
        self._clock = clock

    def set_mark_policy(
        self, *, require_fresh_marks: bool, max_mark_age_seconds: float = 5.0
    ) -> None:
        """Configure the fail-closed live mark dependency."""
        if max_mark_age_seconds < 0:
            raise ValueError("max_mark_age_seconds must be non-negative")
        self._require_fresh_marks = require_fresh_marks
        self._max_mark_age_seconds = float(max_mark_age_seconds)

    def _now(self, value: datetime | None = None) -> datetime:
        """Resolve an evaluation instant through the injected clock seam."""
        if value is not None:
            return value
        if self._clock is not None:
            return self._clock.now()
        raise RuntimeError(
            "RiskManager has no injected clock; "
            "pass clock= or now= to evaluate risk deterministically"
        )

    def _fresh_marks_available(
        self, request: OrderRequest, now: datetime | None,
    ) -> bool:
        """Return whether live opening risk has a trustworthy mark snapshot."""
        if not self._require_fresh_marks:
            return True
        if self._positions_provider is None:
            return False
        instant = self._now(now)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)

        for position in self._positions_provider():
            if position.quantity.value == 0:
                continue
            marked_at = position.marked_at
            if marked_at is None:
                return False
            if marked_at.tzinfo is None:
                marked_at = marked_at.replace(tzinfo=UTC)
            if (instant - marked_at).total_seconds() > self._max_mark_age_seconds:
                return False
            if position.mark_price is None or position.mark_price.value <= 0:
                return False

        # Existing positions and the incoming instrument both need a fresh,
        # positive quote. A request price is not a market mark: accepting a
        # limit order without current market data would make live exposure and
        # daily-loss controls operate on an unverified snapshot.
        if self._price_provider is None:
            return False
        try:
            quote = self._price_provider(request.instrument)
        except Exception:
            return False
        timestamp = getattr(quote, "timestamp", None) if quote is not None else None
        if timestamp is None:
            return False
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        age = (instant - timestamp).total_seconds()
        if age < 0 or age > self._max_mark_age_seconds:
            return False
        if self._mark_price(request.instrument) is None:
            return False
        return True

    def bind_cash_provider(self, provider: Callable[[], Decimal | None]) -> None:
        """Bind a zero-arg callable returning available cash (Decimal).

        When bound, every BUY in :meth:`check` is rejected if its incoming
        notional exceeds the returned cash. SELLs are never cash-gated.
        """
        self._cash_provider = provider

    @property
    def cash_provider_bound(self) -> bool:
        """True when a cash provider is bound (C1 cash gate live)."""
        return getattr(self, "_cash_provider", None) is not None

    @property
    def positions_provider_bound(self) -> bool:
        """True when a positions provider is bound (max_position_value live)."""
        return self._positions_provider is not None

    def check(self, request: OrderRequest, now: datetime | None = None) -> bool:
        """Return True if the order passes risk checks, False to reject.

        Parameters
        ----------
        now : datetime | None
            Evaluation instant for the orders-per-minute window. Defaults to
            wall clock (``datetime.now(UTC)``). A caller replaying a
            deterministic event stream — e.g. BacktestEngine — should pass the
            event timestamp so the rate-limit decision reproduces across runs
            (parity review area #5: deterministic event processing).

        Notes
        -----
        All ``now`` values within one manager must share tz-awareness
        (naive or aware) — the window subtraction compares them directly.
        BacktestEngine resets the window at run start, so one manager is
        dedicated to one mode and never mixes IST tz-naive backtest
        timestamps with aware wall-clock live timestamps.
        """
        with self._lock:
            # G2: resolve the per-strategy (or global) cap values once
            # at the top. Every gate below reads from this tuple, so a
            # request with a matching budget sees its slice, and a
            # request with no budget falls back to the global caps.
            (
                max_order_value,
                max_position_value,
                max_daily_loss_amt,
                max_drawdown_pct,
            ) = self._active_budget(request)

            # C4: defensive tz check. Mixing tz-aware and tz-naive datetimes
            # in the rate-limit window subtraction raises TypeError deep
            # in (now - self._recent_orders[0]).total_seconds(). Validate
            # the awareness of `now` against the window's existing
            # awareness (the first entry in self._recent_orders). When
            # the window is empty, accept whatever awareness the caller
            # passes; a fresh manager has no history to mismatch.
            if now is not None and self._recent_orders:
                if (now.tzinfo is None) != (self._recent_orders[0].tzinfo is None):
                    raise ValueError(
                        "RiskManager.check: tz-awareness mismatch in rate-limit "
                        f"window. now.tzinfo={now.tzinfo!r} but existing entries "
                        f"are "
                        f"{'aware' if self._recent_orders[0].tzinfo else 'naive'}. "
                        "All calls in one session must share tz-awareness "
                        "(see docstring). BacktestEngine passes naive datetimes; "
                        "live wall-clock is aware."
                    )

            # Master gate
            if not self._live_orders_enabled:
                return self._deny("live_orders_disabled", now=now)

            # Short-selling policy gate. A SELL that would open or
            # grow a short is denied unless the account is opted in.
            # A SELL that only reduces a long stays allowed: this gate
            # never blocks flattening. Checked early because it is a
            # hard policy, not a limit that scales with the book.
            if (
                request.side is OrderSide.SELL
                and not self._allow_short
                and self._opens_short(request)
            ):
                return self._deny("short_selling_disabled", now=now)

            # Live opening risk fails closed until the quote-driven MTM
            # service has produced fresh marks for the cached book.
            if self._require_fresh_marks and self._increases_exposure(request):
                if not self._fresh_marks_available(request, now):
                    return self._deny("marks_not_fresh", now=now)

            # Order value check (per-strategy or global)
            if max_order_value is not None:
                mark = self._mark_for_trade(request)
                if mark is None:
                    if self._reject_unknown_market_value:
                        # Fail-closed live: don't let an unpriced MARKET order
                        # slip past the notional gate with no mark to bind it.
                        return self._deny("unknown_market_value", now=now)
                elif mark * request.quantity.value > max_order_value:
                    return self._deny("order_value_exceeded", now=now)

            # Cash check (C1) — only BUY is cash-gated; a SELL is a credit.
            # Incoming notional is what the order would add to exposure.
            # Under ``fail_closed_cash`` an unbound provider, a provider that
            # raises, and a provider that returns None all mean "unknown",
            # and unknown denies rather than admits.
            if request.side is OrderSide.BUY:
                if self._unpriced_cash_check(request):
                    return self._deny("unknown_market_value", now=now)
                provider = getattr(self, "_cash_provider", None)
                if provider is None:
                    if self.fail_closed_cash:
                        return self._deny("cash_unavailable", now=now)
                else:
                    try:
                        cash = provider()  # type: ignore[misc]
                    except Exception:
                        cash = None
                    if cash is None:
                        if self.fail_closed_cash:
                            return self._deny("cash_unavailable", now=now)
                    else:
                        incoming = self._incoming_exposure(request)
                        if incoming > Decimal(str(cash)):
                            return self._deny("insufficient_cash", now=now)

            # Position value check (per-strategy or global)
            if max_position_value is not None:
                exposure = self._position_exposure() + self._incoming_exposure(request)
                if exposure > max_position_value:
                    return self._deny("position_value_exceeded", now=now)

            # Gross / net exposure limits (global). Deny when the book
            # *resulting from this order* would breach either cap. A
            # reduction cannot increase exposure at a fixed mark, so the
            # unconditional check never blocks a flattening — it only
            # stops the book from growing past the cap.
            if (
                self._max_gross_exposure is not None
                or self._max_net_exposure is not None
            ):
                gross, net = self._resulting_exposures(request)
                if (
                    self._max_gross_exposure is not None
                    and gross > self._max_gross_exposure
                ):
                    return self._deny("max_gross_exposure_exceeded", now=now)
                if (
                    self._max_net_exposure is not None
                    and abs(net) > self._max_net_exposure
                ):
                    return self._deny("max_net_exposure_exceeded", now=now)

            # Daily-loss / drawdown guards (per-strategy or global).
            # Only deny new exposure, never a reduction.
            #
            # Both guards read the position book's ``unrealized_pnl``, and a
            # fill resets that to a flat zero (position_math.apply_fill), so a
            # position with no current mark would read as break-even. That is
            # the one reading this block must never act on: fail closed on a
            # stale mark rather than evaluate the guard against a reset zero.
            if (
                max_daily_loss_amt is not None
                or max_drawdown_pct is not None
            ) and self._positions_provider is not None:
                increases = self._increases_exposure(request)
                if self._stale_marks(now) and increases:
                    return self._deny("marks_not_fresh", now=now)
                # AC-9: measured against THIS strategy's own baseline and peak
                # (durable across a restart when a store is bound). Falls back to
                # the account aggregate while the book carries no strategy_id,
                # so the guard is never dead on an unattributed book.
                bucket = (
                    resolve_strategy_id(request)
                    if self._book_is_attributed()
                    else None
                )
                net, peak = self._session_net_for(bucket, now)
                if max_daily_loss_amt is not None and (
                    net <= -max_daily_loss_amt
                ) and increases:
                    return self._deny("daily_loss_exceeded", now=now)
                if max_drawdown_pct is not None:
                    dd = self._drawdown_of(peak, net)
                    if dd is not None and dd >= max_drawdown_pct and increases:
                        return self._deny("drawdown_exceeded", now=now)

            # Rate limit check
            if self._max_orders_per_minute is not None:
                now = self._now(now)

                # Purge old entries
                while self._recent_orders and (now - self._recent_orders[0]).total_seconds() > 60:
                    self._recent_orders.popleft()
                if len(self._recent_orders) >= self._max_orders_per_minute:
                    return self._deny("rate_limit_exceeded", now=now)
                self._recent_orders.append(now)

            # C3: emit exposure/pnl gauges after every approved decision.
            self._emit_gauges(now)
            return True

    def _deny(
        self, reason: str = "risk_check_failed", *, now: datetime | None = None,
    ) -> bool:
        """Record a rejection and return ``False`` (caller returns it)."""
        self._rejected_count += 1
        self._last_deny_reason = reason
        if self._metrics is not None:
            self._metrics.counter(f"orders.rejection_reason.{reason}").inc()
            # Emit gauges on deny too — a breached daily_loss/drawdown must
            # still appear on /metrics, not only after the next approve.
            self._emit_gauges(now)
        return False

    def _emit_gauges(self, now: datetime | None = None) -> None:
        """Emit exposure, daily-loss and drawdown gauges (called under _lock).

        C3: called after every check() decision (approve or deny) so monitoring
        has a fresh reading. ``now`` must match the evaluation instant used by
        the daily-loss/drawdown gates — wall-clock here would roll the session
        baseline on a date mismatch and silently zero the gauges.
        """
        if self._metrics is None:
            return
        exp = self._position_exposure()
        self._metrics.gauge("exposure_notional").set(float(exp))
        if self._positions_provider is not None:
            net = self._session_net(now)
            self._metrics.gauge("daily_loss").set(float(max(Decimal(0), -net)))
            dd = self._drawdown(net)
            self._metrics.gauge("drawdown_pct").set(
                float(dd) if dd is not None else 0.0
            )

    # -- session PnL for daily-loss / drawdown guards ---------------------

    def _stale_marks(self, now: datetime | None) -> bool:
        """True when any open position lacks a *usable* mark right now.

        The daily-loss and drawdown guards read ``unrealized_pnl``, and
        ``tradex_domain.position_math.apply_fill`` deliberately resets that to
        ``0`` on every fill (and clears ``mark_price``/``marked_at``), because
        a mark taken before a fill no longer describes the resulting position.
        That reset is correct accounting — but it means a position that is
        fully under water reads as *flat* until the next quote-driven mark
        arrives. In a halted or circuit-limited name no further tick comes, so
        precisely when the market is most dangerous the drawdown gauge reports
        zero.

        So staleness has to be explicit rather than inferred from the number.
        A position is *usable* only when it is flat, or carries a positive mark
        that is no older than ``max_mark_age_seconds``. A position with an open
        quantity and no such mark is stale, and the guards fail closed on it
        (AC-9): an unknown mark is not a zero mark.

        When ``require_fresh_marks`` is off this still applies, because the
        daily-loss/drawdown gates are themselves an explicit operator decision
        and reading them off a reset zero is the defect being fixed. Backtest
        mode is unaffected in practice: the backtest path passes no positions
        provider, so the block is skipped entirely.
        """
        if self._positions_provider is None:
            return False
        instant = self._now(now)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)
        for pos in self._positions_provider():
            if pos.quantity.value == 0:
                continue
            if pos.mark_price is None or pos.mark_price.value <= 0:
                return True
            marked_at = pos.marked_at
            if marked_at is None:
                return True
            if marked_at.tzinfo is None:
                marked_at = marked_at.replace(tzinfo=UTC)
            age = (instant - marked_at).total_seconds()
            if age < 0 or age > self._max_mark_age_seconds:
                return True
        return False

    def _equity_pnl(self) -> Decimal:
        """Portfolio realized + unrealized PnL from the position book.

        ``0`` when no position provider is bound (guards are then inert).
        """
        if self._positions_provider is None:
            return Decimal(0)
        total = Decimal(0)
        for pos in self._positions_provider():
            total += pos.realized_pnl.amount + pos.unrealized_pnl.amount
        return total

    # -- per-strategy baselines (AC-9) -----------------------------------

    @staticmethod
    def _position_strategy(pos: object) -> str | None:
        """The strategy that owns *pos*, or ``None`` when unattributed.

        ``Position.strategy_id`` is the AC-9 data-model field and is read with
        ``getattr`` so this is correct before and after it lands: a book whose
        positions carry no strategy id is *unattributed*, and every such
        position is measured against the account-level baseline instead. That
        is the honest reading — attributing the whole account to whichever
        strategy happened to ask would be the exact bug AC-9 exists to remove.
        """
        sid = getattr(pos, "strategy_id", None)
        if isinstance(sid, str) and sid.strip():
            return sid.strip()
        return None

    def _book_is_attributed(self) -> bool:
        """True when the position book actually names its owning strategies.

        AC-9 makes ``Position.strategy_id`` part of the data model, and until
        that field exists no position can be attributed. A per-strategy bucket
        over an unattributed book is not a stricter guard, it is a *dead* one:
        the strategy owns nothing, its P&L is zero, and the daily-loss and
        drawdown guards can never fire. So the per-strategy split is used only
        once there is something to split, and the account aggregate is measured
        until then. This is never weaker than the pre-AC-9 behaviour, and it
        becomes per-strategy the moment the field lands — with no change here.
        """
        if self._positions_provider is None:
            return False
        return any(
            self._position_strategy(pos) is not None
            for pos in self._positions_provider()
        )

    def _equity_pnl_for(self, strategy_id: str | None) -> Decimal:
        """PnL of the positions owned by *strategy_id* (or the whole account
        when *strategy_id* is ``None``)."""
        if self._positions_provider is None:
            return Decimal(0)
        total = Decimal(0)
        for pos in self._positions_provider():
            if self._position_strategy(pos) != strategy_id:
                continue
            total += pos.realized_pnl.amount + pos.unrealized_pnl.amount
        return total

    def _load_baselines(self) -> None:
        """Seed the per-strategy baselines from the durable store.

        A stored row for a *previous* date is deliberately dropped rather than
        reused: the day rolls forward exactly as the in-memory path does, so
        yesterday's loss never leaks into today's limit. Only today's rows
        survive a restart, which is the whole point — the breaker that was
        already tripped this morning is still tripped after a crash-restart.
        """
        store = self._baseline_store
        if store is None:
            return
        today = self._now(None).date().isoformat()
        try:
            rows = store.load_all()  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - defensive: never block a check
            log.warning("risk baseline load failed; starting from zero",
                        exc_info=True)
            return
        for sid, baseline in rows.items():
            if baseline.session_date == today:
                self._strategy_baselines[sid] = (
                    date.fromisoformat(today),
                    baseline.base_pnl,
                    baseline.peak_pnl,
                )

    def _persist_baseline(self, sid: str, entry: tuple[date, Decimal, Decimal]) -> None:
        """Advance one strategy's baseline in memory, and to the store if bound.

        The in-memory advance is unconditional — it is what the guards read on
        this session, with or without durability. Only the durable write needs
        a store; without one the peak still advances in RAM exactly as it did
        before this lane, and simply does not survive a restart.
        """
        if self._strategy_baselines.get(sid) == entry:
            return
        self._strategy_baselines[sid] = entry
        store = self._baseline_store
        if store is None:
            return
        try:
            from tradex_execution.risk_baseline_store import StrategyBaseline

            store.save(  # type: ignore[attr-defined]
                StrategyBaseline(
                    strategy_id=sid,
                    session_date=entry[0].isoformat(),
                    base_pnl=entry[1],
                    peak_pnl=entry[2],
                )
            )
        except Exception:  # pragma: no cover - defensive teardown/IO
            log.warning("risk baseline save failed for %s", sid, exc_info=True)

    def _session_net_for(
        self, strategy_id: str | None, now: datetime | None,
    ) -> tuple[Decimal, Decimal]:
        """Return ``(net, peak)`` for one strategy, rolling the day forward.

        A strategy with no stored baseline takes its day-start P&L from the
        current book on first sight, then holds it. When a durable store is
        bound the entry is written out on every change, so a restart resumes
        the same baseline and peak rather than a flattering zero.
        """
        today = self._now(now).date()
        key = strategy_id or ""
        entry = self._strategy_baselines.get(key)
        if entry is None or entry[0] != today:
            entry = (today, self._equity_pnl_for(strategy_id), Decimal(0))
        net = self._equity_pnl_for(strategy_id) - entry[1]
        peak = max(entry[2], net)
        # The unattributed bucket is the account aggregate; it is held in memory
        # (there is no strategy id to key a durable row by) so its day-start
        # baseline is stable for the session. A real strategy id is also written
        # through to the durable store so a restart resumes the same baseline.
        if strategy_id is None:
            self._strategy_baselines[key] = (entry[0], entry[1], peak)
        else:
            self._persist_baseline(strategy_id, (entry[0], entry[1], peak))
        return net, peak

    def _drawdown_of(self, peak: Decimal, net: Decimal) -> Decimal | None:
        """Fractional drawdown from *peak*, or ``None`` at/below zero peak."""
        if peak <= 0:
            return None
        return (peak - net) / peak

    def _session_net(self, now: datetime | None) -> Decimal:
        """Account-level today's PnL relative to the day-start baseline,
        maintaining the day's running peak. Rolls the baseline forward on a
        date change so yesterday's losses never leak into today's limit.
        """
        today = self._now(now).date()

        if self._session_date != today:
            self._session_date = today
            self._base_pnl = self._equity_pnl()
            self._peak_pnl = Decimal(0)
        net = self._equity_pnl() - self._base_pnl
        self._peak_pnl = max(self._peak_pnl, net)
        return net

    def _drawdown(self, net: Decimal) -> Decimal | None:
        """Fractional drawdown from the session peak, or ``None`` at/below 0."""
        if self._peak_pnl <= 0:
            return None
        return (self._peak_pnl - net) / self._peak_pnl

    def _increases_exposure(self, request: OrderRequest) -> bool:
        """True when the order adds gross exposure on *either* side.

        Per-side gross, not net (AC-9 / lane 2f). The net test
        ``new*new > existing*existing`` classified a SELL of 150 against a
        long 100 as a *reduction* (net 100 → 50), which is how the fresh-mark,
        daily-loss and drawdown gates got bypassed by exactly the order that
        flips the book: it opens a brand-new short that no gate had evaluated.

        The result is compared side by side instead. Gross long and gross short
        are the two exposures the account actually carries, and this order
        increases exposure iff it grows *either* of them:

        - long 100, SELL 150 → short 50: gross long 100 → 0, gross short
          0 → 50. The short side grew, so this increases.
        - long 100, SELL 40 → long 60: both sides shrank, so a reduction.
        - long 100, SELL 100 → flat: both sides shrank, so a reduction.
        - short 100, BUY 150 → long 50: gross long 0 → 50, so this increases.
        """
        signed = (
            request.quantity.value
            if request.side is OrderSide.BUY
            else -request.quantity.value
        )
        if self._positions_provider is None:
            # Without a position book we cannot prove this is a reduction.
            # This is intentionally conservative when the fresh-mark gate is
            # enabled; legacy daily-loss checks remain inert without a book.
            return self._require_fresh_marks
        for pos in self._positions_provider():
            if pos.instrument == request.instrument:
                existing = pos.quantity.value
                resulting = existing + signed
                long_before = max(existing, Decimal(0))
                short_before = max(-existing, Decimal(0))
                long_after = max(resulting, Decimal(0))
                short_after = max(-resulting, Decimal(0))
                return (
                    long_after > long_before or short_after > short_before
                )
        return True

    def _opens_short(self, request: OrderRequest) -> bool:
        """True when a SELL would open or grow a short position.

        Uses the same per-side gross arithmetic as
        :meth:`_increases_exposure`: a SELL of 40 against a long 100
        leaves gross short at 0 (a reduction, allowed); a SELL of 150
        against a long 100 grows gross short 0 → 50 (an open, gated).
        Without a position book a SELL cannot be proven to be a reduction, so
        this fails closed unconditionally (True) — deliberately more conservative
        than :meth:`_increases_exposure`'s conditional no-book branch.
        """
        if self._positions_provider is None:
            return True
        signed = -request.quantity.value
        for pos in self._positions_provider():
            if pos.instrument == request.instrument:
                resulting = pos.quantity.value + signed
                short_before = max(-pos.quantity.value, Decimal(0))
                short_after = max(-resulting, Decimal(0))
                return short_after > short_before
        # No position for this instrument: any SELL opens a short.
        return True

    @property
    def rejected_count(self) -> int:
        """Number of orders denied by :meth:`check` since construction."""
        return self._rejected_count

    def reset_rate_window(self) -> None:
        """Clear the orders-per-minute window and rejection counter.

        BacktestEngine calls this at the start of every run so a shared
        RiskManager never leaks rate-limit state between independent
        backtests — results stay a pure function of (data, config,
        strategy) (parity review area #8: reproducible). The reactive/live
        path never calls it, so the live rate limit is unaffected.
        """
        with self._lock:
            self._recent_orders.clear()
            self._rejected_count = 0

    def check_order(self, request: OrderRequest, context: object = None) -> RiskCheckResult:
        """v3-parity risk check returning rich result with the actual deny reason."""
        approved = self.check(request)
        return RiskCheckResult(
            approved=approved,
            reason="" if approved else self._last_deny_reason,
        )
