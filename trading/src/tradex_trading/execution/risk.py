"""Risk gate — extracted from engine.py (PE-6).

Provides the RiskManager with configurable limits: order value, position
value, rate limiting, daily-loss, drawdown, per-strategy budgets, cash
gate, and fresh-mark live safety.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from tradex_domain.enums import OrderSide
from tradex_domain.execution import OrderRequest
from tradex_domain.protocols import Clock

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RiskCheckResult:
    """Outcome of a risk check — approved flag plus human-readable reason."""

    approved: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RiskBudget:
    """Per-strategy slice of the broker-level risk envelope (G2).

    Each strategy on the same account draws from its own budget so a
    buggy strategy on instrument A can never exhaust the limit for
    strategy B on instrument C. Strategies with no budget (legacy /
    ad-hoc orders) fall back to the global caps on ``RiskManager``.
    """

    strategy_id: str
    max_order_value: Decimal | None = None
    max_position_value: Decimal | None = None
    max_daily_loss_amt: Decimal | None = None
    max_drawdown_pct: Decimal | None = None


class RiskManager:
    """Simple risk manager with configurable limits."""

    def __init__(
        self,
        max_order_value: Decimal | None = None,
        max_position_value: Decimal | None = None,
        max_orders_per_minute: int | None = None,
        *,
        live_orders_enabled: bool = True,
        positions_provider: Any | None = None,
        price_provider: Any | None = None,
        reject_unknown_market_value: bool = False,
        require_fresh_marks: bool = False,
        max_mark_age_seconds: float = 5.0,
        max_daily_loss_amt: Decimal | None = None,
        max_drawdown_pct: Decimal | None = None,
        budgets: dict[str, RiskBudget] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._max_order_value = max_order_value
        self._max_position_value = max_position_value
        self._max_orders_per_minute = max_orders_per_minute
        self._recent_orders: deque[datetime] = deque()
        self._lock = threading.Lock()
        self._live_orders_enabled = live_orders_enabled
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
        # M5: real type (was ``Any``, defeating mypy). The field stores
        # the calendar date of the day the position baselines were taken,
        # used to roll the day-start P&L forward on a date change.
        self._session_date: date | None = None
        self._base_pnl = Decimal("0")
        self._peak_pnl = Decimal("0")
        #: Callable returning current positions (e.g. an OMS cache) so
        #: ``max_position_value`` can be enforced against live exposure. When
        #: None (backtest boot, unit tests), the position check is skipped.
        self._positions_provider = positions_provider
        #: Callable mapping an instrument to its current market price (Price
        #: or Decimal). When None (or when it yields no usable price),
        #: exposure falls back to the position's avg_price.
        self._price_provider = price_provider
        #: G2: per-strategy risk budgets. ``check()`` resolves the active
        #: budget from ``request.tag`` (the strategy_id; the strategy engine
        #: already stamps ``strategy_id@version`` into tag). A request with
        #: no matching budget falls back to the global caps above.
        self._budgets: dict[str, RiskBudget] = dict(budgets or {})
        self._clock = clock
        #: Callable returning the current cash balance (Decimal). Bound
        #: lazily via ``bind_cash_provider()`` so the engine can boot
        #: without a cash ledger (backtest, unit tests). Initialized to
        #: None explicitly (M9 fix — previously relied on getattr fallback).
        self._cash_provider: Any | None = None
        #: Count of orders denied by ``check()`` (any gate). Read by
        #: BacktestEngine to populate ``BacktestResult.num_rejected`` without
        #: re-implementing rejection bookkeeping in its own loop.
        self._rejected_count = 0

    def _active_budget(self, request: Any) -> tuple[Decimal | None, ...]:
        """Resolve the active cap values for ``request``.

        Returns ``(max_order_value, max_position_value,
        max_daily_loss_amt, max_drawdown_pct)`` for the strategy, or
        the global caps if the strategy has no budget. G2.
        """
        sid = (request.tag or "").strip() if request is not None else ""
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
        total = Decimal("0")
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
        """Notional of the incoming order (price * quantity)."""
        mark = self._mark_for_trade(request)
        if mark is None:
            return Decimal("0")
        return mark * request.quantity.value

    @property
    def live_orders_enabled(self) -> bool:
        """Master gate: when False, all orders are rejected."""
        return self._live_orders_enabled

    @live_orders_enabled.setter
    def live_orders_enabled(self, value: bool) -> None:
        self._live_orders_enabled = value

    def set_positions_provider(self, provider: Any) -> None:
        """Bind the position source used for ``max_position_value``.

        ``provider`` is a zero-arg callable returning an iterable of
        positions (e.g. ``engine.cache.all_positions``). When unset the
        position check is skipped.
        """
        self._positions_provider = provider

    def set_price_provider(self, provider: Any) -> None:
        """Bind a market-price source for exposure marking.

        ``provider`` is a one-arg callable mapping an instrument to its
        current price (``Price`` or ``Decimal``). When unset — or when it
        returns nothing usable — exposure falls back to avg_price.
        """
        self._price_provider = provider

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
        return datetime.now(UTC)

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

    def bind_cash_provider(self, provider: Any) -> None:
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
                return self._deny()

            # Live opening risk fails closed until the quote-driven MTM
            # service has produced fresh marks for the cached book.
            if self._require_fresh_marks and self._increases_exposure(request):
                if not self._fresh_marks_available(request, now):
                    return self._deny()

            # Order value check (per-strategy or global)
            if max_order_value is not None:
                mark = self._mark_for_trade(request)
                if mark is None:
                    if self._reject_unknown_market_value:
                        # Fail-closed live: don't let an unpriced MARKET order
                        # slip past the notional gate with no mark to bind it.
                        return self._deny()
                elif mark * request.quantity.value > max_order_value:
                    return self._deny()

            # Cash check (C1) — only BUY is cash-gated; a SELL is a credit.
            # Skipped when no cash provider is bound (backward-compat with
            # risk configs that do not track cash). Incoming notional is
            # the notional the order would add to existing exposure.
            if (
                request.side is OrderSide.BUY
                and getattr(self, "_cash_provider", None) is not None
            ):
                try:
                    cash = self._cash_provider()
                except Exception:
                    cash = None
                if cash is not None:
                    incoming = self._incoming_exposure(request)
                    if incoming > Decimal(str(cash)):
                        return self._deny()

            # Position value check (per-strategy or global)
            if max_position_value is not None:
                exposure = self._position_exposure() + self._incoming_exposure(request)
                if exposure > max_position_value:
                    return self._deny()

            # Daily-loss / drawdown guards (per-strategy or global).
            # Only deny new exposure, never a reduction.
            if (
                max_daily_loss_amt is not None
                or max_drawdown_pct is not None
            ) and self._positions_provider is not None:
                net = self._session_net(now)
                if max_daily_loss_amt is not None and (
                    net <= -max_daily_loss_amt
                ) and self._increases_exposure(request):
                    return self._deny()
                if max_drawdown_pct is not None:
                    dd = self._drawdown(net)
                    if dd is not None and dd >= max_drawdown_pct \
                            and self._increases_exposure(request):
                        return self._deny()

            # Rate limit check
            if self._max_orders_per_minute is not None:
                now = self._now(now)

                # Purge old entries
                while self._recent_orders and (now - self._recent_orders[0]).total_seconds() > 60:
                    self._recent_orders.popleft()
                if len(self._recent_orders) >= self._max_orders_per_minute:
                    return self._deny()
                self._recent_orders.append(now)

            return True

    def _deny(self) -> bool:
        """Record a rejection and return ``False`` (caller returns it)."""
        self._rejected_count += 1
        return False

    # -- session PnL for daily-loss / drawdown guards ---------------------

    def _equity_pnl(self) -> Decimal:
        """Portfolio realized + unrealized PnL from the position book.

        ``0`` when no position provider is bound (guards are then inert).
        """
        if self._positions_provider is None:
            return Decimal("0")
        total = Decimal("0")
        for pos in self._positions_provider():
            total += pos.realized_pnl.amount + pos.unrealized_pnl.amount
        return total

    def _session_net(self, now: datetime | None) -> Decimal:
        """Today's PnL relative to the day-start baseline, maintaining the
        day's running peak. Rolls the baseline forward on a date change so
        yesterday's losses never leak into today's limit.
        """
        today = self._now(now).date()

        if self._session_date != today:
            self._session_date = today
            self._base_pnl = self._equity_pnl()
            self._peak_pnl = Decimal("0")
        net = self._equity_pnl() - self._base_pnl
        if net > self._peak_pnl:
            self._peak_pnl = net
        return net

    def _drawdown(self, net: Decimal) -> Decimal | None:
        """Fractional drawdown from the session peak, or ``None`` at/below 0."""
        if self._peak_pnl <= 0:
            return None
        return (self._peak_pnl - net) / self._peak_pnl

    def _increases_exposure(self, request: OrderRequest) -> bool:
        """True when the order grows the existing position or opens one."""
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
                new_signed = existing + signed
                return new_signed * new_signed > existing * existing
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

    def check_order(self, request: OrderRequest, context: Any = None) -> RiskCheckResult:
        """v3-parity risk check returning rich result."""
        approved = self.check(request)
        return RiskCheckResult(
            approved=approved,
            reason="" if approved else "risk_check_failed",
        )
