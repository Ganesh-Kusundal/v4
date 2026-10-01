"""Position sizing — the one place a signal becomes an order size.

**Why this module exists.** The size used to be ``abs(signal.strength)`` in the
strategy engine and, verbatim, in the backtest's recorded-signal bridge.
``Signal.strength`` is signal *quality*; every shipped strategy emits
``strength=1.0``; so every strategy in this repository bought exactly one share,
and a strategy that computed ``strength = 3 * sigma`` would have silently
tripled its position. Sizing is not a by-product of a signal field, it is a
decision with a target, and the target is fixed (spec 0002, Flow C step 1 /
AC-5)::

    lots      = min( max_order_value / (lot_size x price),
                    risk_fraction x equity / (stop_distance x tick_size x lot_size) )
    quantity  = max(lots, 1) x lot_size

``max_order_value`` is a notional in rupees and the second term is a count of
lots, so the minimum is taken over **notionals** — each term divided by the
reference price — which is the same comparison written in one unit. The tick
appears in the denominator as a product with the stop distance because the
distance is a number of ticks: ``ticks x tick_size`` is the rupee distance
between entry and stop, and the venue cannot stop you out between ticks, so the
distance is rounded **up** to a whole tick (understating the risk per lot would
size the trade on risk the stop cannot cause).

**The denominator is sometimes not knowable, and then the sizer does not
decide.** A signal with no declared stop, or a contract whose specification no
master has recorded, has no risk denominator. There are two honest answers and
this module takes the *configured* one: a ``fallback_notional`` supplied by the
operator sizes the order to that notional, floored to one lot and capped by
``max_order_value``. With no fallback configured the sizer **refuses** and the
engine drops the signal with an error. It never returns a default quantity —
a sizer that quietly answers "one" when it cannot decide is the same defect
wearing a different hat.

**This is not a second way for a strategy to choose its own size.** A strategy
states intent with ``Signal.quantity`` (or leaves it null and takes the risk
size), and everything after that is this module's: whole lots, the notional
ceiling, and the one-lot floor. A strategy can make its position smaller than
the sizer would; it can never make it larger than ``max_order_value``.

**One declaration, two readers.** The stop that sizes the trade is the same
``metadata['stop_loss_price']`` the engine turns into the protective leg (see
``brackets.py``), read through the same ``declared_levels`` normaliser. If the
sizer could read a stop the order does not carry, the position would be sized
against a risk that is not protected.

**One source of truth, two modes** (C2, lane A)
-------------------------------------------------
``backtest.py`` builds a risk-budget sizer from ``(max_order_value,
risk_fraction, equity=initial_capital)`` and hands it to its
``ReactiveStrategyEngine``, while the live/paper composition root passed no
``sizer=`` at all — so ``ReactiveStrategyEngine`` fell back to
:func:`unconfigured_sizer`, which has no risk fraction, no equity, and a
hardcoded notional. The same signal was therefore sized two different ways in
the two modes, and **a backtest that sizes differently from live is not
evidence about live behaviour**. :func:`account_sizer` is the one construction
both modes now use: the composition root builds it once from the account's
inputs and hands *that object* to both the live and the backtest engine, so the
two cannot drift. It mirrors ``BacktestEngine._resolve_sizer`` field for field
(including ``fallback_notional=max_order_value``), which is what makes the
parity control in ``trading/tests/strategy/test_sizing_parity.py`` assert
equality of the produced quantities rather than of the configuration.

**Where lot and tick actually live.** The Dhan and Upstox master loaders park
them in the registry's row meta as *strings* and never populate the typed
``Instrument.lot_size`` / ``Instrument.tick_size`` fields, so a sizer that
reads only the typed fields sees ``None`` for every real symbol. Resolution
order is therefore: the ``contracts`` lookup the wiring supplies (the registry's
own answer), then the typed instrument field, then ``InstrumentMeta``, then the
master's ``meta.extra``.

**What "unknown" means, which is not the same thing for both of them.** The two
values are both venue-published and neither is derivable, and they are treated
differently because the *cost of being wrong* is different:

* **Lot size.** For a **cash equity** a missing lot size is **not** unknown:
  NSE/BSE cash equities trade in units of one share, so ``1`` is the venue's
  rule rather than a guess, and the answer is the same for every cash equity
  regardless of which security it is. For **everything else** — futures,
  options, currency and commodity contracts — the lot size is a property of
  the individual contract, it changes between expiries and strikes and between
  brokers, and nothing about the instrument implies it. So a contract with no
  recorded specification still leaves the denominator not knowable, and the
  sizer still falls back or refuses. Sizing a derivative on an invented lot is
  how money is lost, and that refusal is the whole reason
  :data:`CASH_EQUITY_LOT_SIZE` is scoped to :data:`CASH_EQUITY_ASSET_CLASSES`
  rather than applied to anything with a missing field.
* **Tick size** is not derivable for *any* instrument, so it comes from
  configuration (:data:`SIZING_FALLBACK_TICK`, overridable per sizer) and the
  substitution is recorded on the decision (``tick_size``,
  ``tick_size_defaulted``) instead of passing for a real venue tick. The default
  is the smallest NSE cash tick, which bounds the error in the rounded stop
  distance to one paisa: the rounded distance is always at least the true
  distance, so a tick that is too small costs a paisa and a tick that is too
  large would size the trade on risk the stop cannot reach. The domain-wide
  canonical tick is :data:`tradex_domain.constants.DEFAULT_TICK_SIZE` (0.05,
  the NSE equity tick); :data:`SIZING_FALLBACK_TICK` is the sizer's own
  fallback for when no master describes the instrument's tick.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Final

from tradex_domain.enums import AssetClass
from tradex_domain.strategy import Signal
from tradex_domain.value_objects import Price, Quantity

from tradex_strategy.core.brackets import declared_levels

log = logging.getLogger(__name__)

#: The lot size of an NSE/BSE **cash equity**, which is one share. Not a guess
#: and not a preference: the cash segment has no lot, so the venue's own rule
#: fixes this to 1 for every cash equity on every exchange this platform trades.
#: It is therefore a constant rather than a constructor argument — a policy knob
#: here would be a knob nobody could turn in the right direction.
CASH_EQUITY_LOT_SIZE: Final = Decimal("1")

#: The asset classes the rule above applies to, and *only* them. Every other
#: class is a contract whose lot size is published per instrument, so a missing
#: one stays unknown and the sizer refuses.
CASH_EQUITY_ASSET_CLASSES: Final = frozenset({AssetClass.EQUITY})

#: The tick size assumed when no master recorded one. The NSE cash tick is 0.01
#: for securities under Rs 250 and larger above it, so this is the smallest
#: common value: the rounded stop distance is ``ceil(distance / tick) * tick``,
#: which is never below the true distance, and a tick that is too small bounds
#: the overstatement at one tick of the security's real grid instead of
#: understating risk by up to one tick of it.
SIZING_FALLBACK_TICK: Final = Decimal("0.01")

#: Where a size came from. Recorded on every decision so an order's size can be
#: traced to the constraint that produced it in the audit trail.
SOURCE_SIGNAL: Final = "signal"  # the strategy stated Signal.quantity
SOURCE_STOP_DISTANCE: Final = "stop_distance"  # the risk formula, against the declared stop
SOURCE_FALLBACK_NOTIONAL: Final = "fallback_notional"  # a configured notional, no risk denominator
SOURCE_REFUSED: Final = "refused"  # no size could be decided; the order must not exist

#: Which constraint bound the result. Exactly one, and named so a test can prove
#: *which* rule fired rather than merely that some size came out.
CLAMP_MAX_ORDER_VALUE: Final = "max_order_value"  # the notional ceiling bound it
CLAMP_LOT_FLOOR: Final = "lot_floor"  # raised to the venue's one-lot minimum
CLAMP_LOT_ROUND: Final = "lot_round"  # a stated quantity rounded down to a whole lot

#: The notional ceiling an engine uses when nothing has been wired into it: a
#: fixed Rs 100,000 order, lot-floored and tick-clamped, with no equity behind it
#: because there is no account to read one to size against. It exists so the
#: interim engine places a *real* size instead of one share or nothing, and it is
#: a placeholder — the wiring is expected to replace it with the configured
#: policy. Every decision taken under it says so in ``reason``, and a configured
#: sizer is always available to callers that care.
DEFAULT_MAX_ORDER_VALUE: Final = Decimal("100000")

#: What share of the ceiling :func:`unconfigured_sizer` actually places, and why
#: it is not all of it. Fees (STT, stamp duty, brokerage, GST) are charged on
#: top of an order's *notional*, and the engine's affordability floor is
#: notional + fee. So a fallback notional **equal** to the ceiling is
#: unaffordable for any BUY in an account holding exactly the ceiling — which is
#: the default paper account, whose opening cash is the same Rs 100,000. That is
#: not a rounding detail: it made the placeholder's one promise false. It
#: promised "a real size instead of one share or nothing" and produced *nothing*
#: — every BUY it sized was rejected by the cash gate with an error, and a
#: SELL-only strategy was the only kind that ever filled. 5% of headroom is
#: wider than the fee load on a delivery order by two orders of magnitude, so the
#: order it does place is comfortably fundable, and it is still a real order
#: rather than a token one.
UNCONFIGURED_FALLBACK_FRACTION: Final = Decimal("0.95")


def account_sizer(
    *,
    equity: Any,
    max_order_value: Any,
    risk_fraction: Any | None,
    equity_source: Callable[[], Decimal | None] | None = None,
    contracts: Callable[[Any], ContractSpec | None] | None = None,
    tick_size: Any | None = None,
    label: str = "account",
) -> PositionSizer:
    """Build **the** account ``PositionSizer`` — one source of truth for sizing.

    This is the fix for the parity break: ``backtest.py`` constructs a
    risk-budget sizer from ``(max_order_value, risk_fraction,
    equity=initial_capital)`` and hands it to its ``ReactiveStrategyEngine``,
    while ``startup.py`` passed no ``sizer=`` at all, so the live/paper engine
    fell back to :func:`unconfigured_sizer` — no risk fraction, no equity, a
    hardcoded notional. The same signal was therefore sized two different ways
    in the two modes, and a backtest that sizes differently from live is not
    evidence about live behaviour. (Measured on the shipped ``sma_cross``:
    8,333 shares backtest vs 7,916 live.)

    The fix is not a second formula here, it is **this one factory** so both
    engines are handed the *same object*, built from the same account inputs.
    Wave 2 constructs it once at the composition root and passes it to both
    ``ReactiveStrategyEngine(sizer=...)`` and ``BacktestEngine(sizer=...)``.

    The parameters deliberately mirror :meth:`BacktestEngine._resolve_sizer`
    field for field — ``max_order_value`` the notional ceiling, ``risk_fraction``
    the risk budget, ``equity`` the account balance — so a sizer built here and
    one built by the backtest from the same three numbers are the same sizer.
    ``fallback_notional`` is wired to ``max_order_value``, matching the
    backtest, so a signal with no knowable risk denominator takes the same
    notional ceiling in both modes.

    Args:
        equity: account equity in rupees, or a live reading. For **backtest**
            this is the run's *opening* capital (sizing against a balance the
            run's own P&L produced is a feedback loop). For **live/paper** pass
            the account's current balance via ``equity_source`` so each signal is
            sized against the balance the session actually holds.
        max_order_value: per-order notional ceiling in rupees; required.
        risk_fraction: fraction of equity one trade may lose. ``None``
            disables the risk formula (the sizer falls back to the notional).
        equity_source: live balance reader, read at sizing time; takes
            precedence over ``equity``. ``None`` means the balance could not
            be read, which disables the risk formula rather than failing the
            order. Omit for backtest.
        contracts, tick_size: passed through to ``PositionSizer`` unchanged.
        label: names this sizer in log lines.

    Returns:
        A configured ``PositionSizer`` identical to what the backtest builds
        from the same inputs.
    """
    return PositionSizer(
        max_order_value=max_order_value,
        risk_fraction=risk_fraction,
        equity=equity,
        equity_source=equity_source,
        fallback_notional=max_order_value,
        contracts=contracts,
        tick_size=tick_size,
        label=label,
    )


def unconfigured_sizer() -> PositionSizer:
    """The sizer an engine falls back to when it was given none.

    Notional-only, deliberately: without an account there is no equity to risk a
    fraction of and no master row to read a lot size from, so the only size this
    can justify is a configured one. Returns a fresh instance rather than a
    shared singleton so a caller can subclass or re-point it without a global.

    This is a **placeholder**, and it says so at construction: a notional-only
    size is a real order, and an operator who has not wired the account policy
    should learn that from the log rather than from a fill. It places
    :data:`DEFAULT_MAX_ORDER_VALUE` x :data:`UNCONFIGURED_FALLBACK_FRACTION`,
    not the whole ceiling — see that constant for why the difference is the whole
    difference between an order and no order.
    """
    fallback = (DEFAULT_MAX_ORDER_VALUE * UNCONFIGURED_FALLBACK_FRACTION).quantize(
        Decimal("1"),
    )
    log.warning(
        "no PositionSizer was configured for this engine; sizing to a fixed "
        "%s notional per order instead of a risk budget. Wire the account's "
        "policy (max_order_value, risk_fraction, equity) at the composition root.",
        fallback,
    )
    return PositionSizer(
        max_order_value=DEFAULT_MAX_ORDER_VALUE,
        fallback_notional=fallback,
        label="unconfigured",
    )


@dataclass(frozen=True, slots=True)
class ContractSpec:
    """Lot and tick for one instrument, as the venue's master recorded them."""

    lot_size: Decimal
    tick_size: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SizingDecision:
    """The outcome of one sizing request.

    ``quantity is None`` means **refused**: the sizer could not decide a size and
    the order must not be built. Every refusal carries the reason, because a
    dropped signal is otherwise indistinguishable from a quiet strategy.
    """

    quantity: Quantity | None
    source: str
    reason: str
    lots: Decimal | None = None
    notional: Decimal | None = None
    clamped_by: str | None = None
    stop_distance_ticks: Decimal | None = None
    risk_per_lot: Decimal | None = None
    #: The tick the stop distance was rounded against, and whether that tick is
    #: the configured default rather than something a master published. Without
    #: the flag, a decision taken on a default tick reads exactly like one taken
    #: on the venue's real grid.
    tick_size: Decimal | None = None
    tick_size_defaulted: bool = False

    @property
    def ok(self) -> bool:
        return self.quantity is not None

    def __bool__(self) -> bool:
        return self.ok

    @classmethod
    def refused(cls, reason: str) -> SizingDecision:
        return cls(quantity=None, source=SOURCE_REFUSED, reason=reason)


def _positive(value: Any) -> Decimal | None:
    """*value* as a finite, strictly positive ``Decimal``, else ``None``."""
    if value is None:
        return None
    raw: Any = value.value if isinstance(value, (Price, Quantity)) else value
    try:
        amount = raw if isinstance(raw, Decimal) else Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, TypeError, AttributeError):
        return None
    return amount if amount.is_finite() and amount > 0 else None


def _whole_lots(units: Decimal, lot: Decimal | None) -> tuple[Decimal, str | None]:
    """*units* rounded down to a whole number of *lot*, with the clamp named."""
    if lot is None:
        return units, None
    lots = (units / lot).to_integral_value(rounding=ROUND_FLOOR)
    if lots * lot == units:
        return units, None
    return lots * lot, CLAMP_LOT_ROUND


class PositionSizer:
    """Sizes a signal, with a target fixed by the spec and no discretion.

    Construct with the account's policy — ``max_order_value`` (the per-order
    notional ceiling), ``risk_fraction`` and ``equity`` for the risk formula,
    and ``fallback_notional`` for signals that have no knowable denominator.
    Everything except ``max_order_value`` is optional, and a sizer without the
    risk inputs simply does not use the risk formula.
    """

    def __init__(
        self,
        *,
        max_order_value: Decimal,
        risk_fraction: Decimal | None = None,
        equity: Decimal | None = None,
        equity_source: Callable[[], Decimal | None] | None = None,
        fallback_notional: Decimal | None = None,
        contracts: Callable[[Any], ContractSpec | None] | None = None,
        tick_size: Decimal | None = None,
        label: str = "sizer",
    ) -> None:
        """Bind the sizing policy.

        Args:
            max_order_value: per-order notional ceiling in rupees; required,
                because it is the one limit that must never be unbounded.
            risk_fraction: fraction of equity one trade may lose. Required for
                the risk formula; absent or non-positive disables it.
            equity: account equity in rupees, used when ``equity_source`` is
                unset. Absent or non-positive disables the risk formula.
            equity_source: reads equity at sizing time, so a long-running live
                session sizes against the balance it actually has rather than
                the one it booted with. Takes precedence over ``equity``.
                Returning ``None`` (an unreadable account) is not a crash: it
                disables the risk formula and the sizer takes its configured
                fallback notional, saying so in the log.
            fallback_notional: notional to use when the risk denominator is not
                knowable. ``None`` (the default) means refuse instead.
            contracts: resolves an ``InstrumentId`` to its ``ContractSpec`` —
                the registry's own answer, for instruments whose typed fields
                the master loader never filled.
            tick_size: the tick to round a stop distance against for instruments
                no master described. ``None`` means :data:`SIZING_FALLBACK_TICK`.
                A non-positive value is an error rather than a silent fallback,
                because a typo that reads as a policy is worse than a refusal.
            label: names this sizer in log lines.
        """
        ceiling = _positive(max_order_value)
        if ceiling is None:
            raise ValueError(f"max_order_value is not a positive amount: {max_order_value!r}")
        configured_tick = _positive(tick_size) if tick_size is not None else None
        if tick_size is not None and configured_tick is None:
            raise ValueError(f"tick_size is not a positive amount: {tick_size!r}")
        self._max_order_value: Decimal = ceiling
        self._tick_size: Decimal = configured_tick or SIZING_FALLBACK_TICK
        self._risk_fraction = _positive(risk_fraction)
        self._equity = _positive(equity)
        self._equity_source = equity_source
        self._fallback_notional = _positive(fallback_notional)
        self._contracts = contracts
        self._label = label

    # -- the policy, readable ------------------------------------------------

    @property
    def max_order_value(self) -> Decimal:
        """The per-order notional ceiling every size is capped by."""
        return self._max_order_value

    @property
    def tick_size(self) -> Decimal:
        """The tick used for instruments no master described."""
        return self._tick_size

    @property
    def fallback_notional(self) -> Decimal | None:
        """The notional used when there is no knowable denominator, if any."""
        return self._fallback_notional

    @property
    def label(self) -> str:
        return self._label

    def equity(self) -> Decimal | None:
        """Current equity, or ``None`` when no risk budget can be read."""
        if self._equity_source is not None:
            try:
                return _positive(self._equity_source())
            except Exception:  # noqa: BLE001 — a broken account read is not a crash
                log.exception("%s: equity_source raised; no risk budget", self._label)
                return None
        return self._equity

    # -- contract data -------------------------------------------------------

    def contract(self, instrument: Any) -> ContractSpec | None:
        """Lot and tick for *instrument*, from wherever the master recorded them.

        ``None`` means the *lot* size could not be established, which is the
        only thing that makes the risk denominator unknowable. A cash equity
        always yields a spec: see :data:`CASH_EQUITY_LOT_SIZE` for why a missing
        lot size is a rule rather than a gap there, and why it is not one
        anywhere else.
        """
        if instrument is None:
            return None
        if self._contracts is not None:
            try:
                spec = self._contracts(instrument.instrument_id)
            except Exception:  # noqa: BLE001 — a failed lookup is a missing answer
                log.exception("%s: contract lookup failed for %r", self._label, instrument)
                spec = None
            if spec is not None and _positive(spec.lot_size) is not None:
                return ContractSpec(
                    lot_size=_positive(spec.lot_size),  # type: ignore[arg-type]
                    tick_size=_positive(spec.tick_size),
                )
        lot = _positive(getattr(instrument, "lot_size", None))
        tick = _positive(getattr(instrument, "tick_size", None))
        if lot is None or tick is None:
            meta = getattr(instrument, "meta", None)
            if lot is None:
                lot = _positive(getattr(meta, "lot_size", None))
            if tick is None:
                tick = _positive(getattr(meta, "tick_size", None))
            if lot is None or tick is None:
                extra = getattr(meta, "extra", None)
                if isinstance(extra, dict):
                    if lot is None:
                        lot = _positive(extra.get("lot_size"))
                    if tick is None:
                        tick = _positive(extra.get("tick_size"))
        if lot is None:
            if getattr(instrument, "asset_class", None) not in CASH_EQUITY_ASSET_CLASSES:
                return None
            log.debug(
                "%s: no master recorded a lot size for %s, and a cash equity's "
                "lot size is %s by the venue's rule; a contract with no recorded "
                "specification would have been refused here",
                self._label, getattr(instrument, "symbol", "?"), CASH_EQUITY_LOT_SIZE,
            )
            lot = CASH_EQUITY_LOT_SIZE
        return ContractSpec(lot_size=lot, tick_size=tick)

    # -- the decision --------------------------------------------------------

    def size(self, signal: Signal, *, reference_price: Any) -> SizingDecision:
        """The order size for *signal* at *reference_price*.

        *reference_price* is the price the order will actually carry, because
        both the notional ceiling and the risk budget are prices: sized against
        the signal bar's close, a ``next_open`` order is sized against a price it
        never got.
        """
        price = _positive(reference_price)
        if price is None:
            return SizingDecision.refused(
                f"{self._label}: reference price {reference_price!r} is not a usable "
                "price, so no notional can be computed"
            )

        instrument = signal.instrument
        spec = self.contract(instrument)
        lot = spec.lot_size if spec is not None else None
        tick = spec.tick_size if spec is not None else None
        symbol = getattr(instrument, "symbol", "?")
        tick_defaulted = tick is None
        if tick_defaulted:
            tick = self._tick_size

        # One lot must be affordable inside the ceiling, or the two constraints
        # are irreconcilable and the honest answer is no order. Flooring to one
        # lot here would breach the very limit the sizer exists to enforce.
        if self._max_order_value < price * (lot if lot is not None else Decimal(1)):
            return SizingDecision.refused(
                f"{self._label}: max_order_value {self._max_order_value} cannot cover "
                f"one lot of {symbol} at {price}"
            )

        stated = _positive(getattr(signal, "quantity", None))
        if stated is not None:
            return self._from_stated(
                stated=stated, lot=lot, tick=tick, price=price, symbol=symbol,
                signal=signal, tick_defaulted=tick_defaulted,
            )
        return self._from_risk(
            signal=signal, lot=lot, tick=tick, price=price, symbol=symbol,
            tick_defaulted=tick_defaulted,
        )

    # -- internals -----------------------------------------------------------

    def _from_stated(
        self, *, stated: Decimal, lot: Decimal | None, tick: Decimal | None,
        price: Decimal, symbol: str, signal: Signal, tick_defaulted: bool,
    ) -> SizingDecision:
        """Honour an explicit ``Signal.quantity``, still clamped by everything."""
        units, rounded_by = _whole_lots(stated, lot)
        if lot is None:
            log.warning(
                "%s: %s has no recorded lot size, so the stated quantity %s is "
                "passed as a whole number of units and the venue's own lot check "
                "is the last word",
                self._label, symbol, stated,
            )
        if units <= 0:
            return SizingDecision.refused(
                f"{self._label}: stated quantity {stated} of {symbol} rounds to no "
                f"whole lot of {lot}"
            )
        stop, _target = declared_levels(getattr(signal, "metadata", None))
        distance = abs(price - stop.value) if stop is not None else None
        return self._cap(
            units=units, lot=lot, price=price, symbol=symbol,
            source=SOURCE_SIGNAL,
            reason=f"{self._label}: strategy stated quantity {stated}",
            clamped_by=rounded_by,
            stop_distance_ticks=(
                self._ticks(distance, tick) if distance is not None and distance > 0 else None
            ),
            risk_per_lot=None,
            tick=tick,
            tick_defaulted=tick_defaulted,
        )

    def _from_risk(
        self, *, signal: Signal, lot: Decimal | None, tick: Decimal | None,
        price: Decimal, symbol: str, tick_defaulted: bool,
    ) -> SizingDecision:
        """The risk formula against the declared stop, or the honest fallback."""
        equity = self.equity()
        stop, _target = declared_levels(getattr(signal, "metadata", None))
        distance = abs(price - stop.value) if stop is not None else None
        missing: list[str] = []
        if self._risk_fraction is None:
            missing.append("no risk_fraction configured")
        if equity is None:
            missing.append("no positive equity to risk a fraction of")
        if lot is None:
            missing.append(f"no lot size recorded for {symbol}")
        if stop is None:
            missing.append("no stop declared, so there is no risk distance")
        elif distance is None or distance <= 0:
            missing.append("the declared stop is at the entry, so there is no risk distance")

        if missing:
            return self._fallback(
                why="; ".join(missing), lot=lot, price=price, symbol=symbol,
                tick=tick, tick_defaulted=tick_defaulted,
            )

        assert lot is not None and equity is not None and distance is not None
        ticks = self._ticks(distance, tick)
        risk_per_lot = ticks * (tick if tick is not None else Decimal(1)) * lot
        budget = self._risk_fraction * equity  # type: ignore[operator]
        lots = budget / risk_per_lot
        units = (lots * lot).to_integral_value(rounding=ROUND_FLOOR)
        return self._cap(
            units=units, lot=lot, price=price, symbol=symbol,
            source=SOURCE_STOP_DISTANCE,
            reason=(
                f"{self._label}: {self._risk_fraction} of {equity} equity is {budget}, "
                f"and the declared stop risks {risk_per_lot} per lot of {symbol}"
            ),
            clamped_by=None,
            stop_distance_ticks=ticks,
            risk_per_lot=risk_per_lot,
            tick=tick,
            tick_defaulted=tick_defaulted,
        )

    def _fallback(
        self, *, why: str, lot: Decimal | None, price: Decimal, symbol: str,
        tick: Decimal | None, tick_defaulted: bool,
    ) -> SizingDecision:
        """Size to the configured notional, or refuse. Never a default quantity."""
        if self._fallback_notional is None:
            return SizingDecision.refused(
                f"{self._label}: cannot size {symbol} — {why} — and no "
                "fallback_notional is configured"
            )
        log.warning(
            "%s: sizing %s to the configured fallback notional %s because %s",
            self._label, symbol, self._fallback_notional, why,
        )
        units = (self._fallback_notional / price).to_integral_value(rounding=ROUND_FLOOR)
        rounded, rounded_by = _whole_lots(units, lot)
        return self._cap(
            units=rounded, lot=lot, price=price, symbol=symbol,
            source=SOURCE_FALLBACK_NOTIONAL,
            reason=f"{self._label}: {why}; sized to the configured notional "
                   f"{self._fallback_notional}",
            clamped_by=rounded_by,
            stop_distance_ticks=None,
            risk_per_lot=None,
            tick=tick,
            tick_defaulted=tick_defaulted,
        )

    def _cap(
        self, *, units: Decimal, lot: Decimal | None, price: Decimal, symbol: str,
        source: str, reason: str, clamped_by: str | None,
        stop_distance_ticks: Decimal | None, risk_per_lot: Decimal | None,
        tick: Decimal | None, tick_defaulted: bool,
    ) -> SizingDecision:
        """Apply the notional ceiling, then the one-lot floor. Order matters."""
        cap = (self._max_order_value / price).to_integral_value(rounding=ROUND_FLOOR)
        if lot is not None:
            cap = (cap / lot * lot).to_integral_value(rounding=ROUND_FLOOR)
        if units > cap:
            units, clamped_by = cap, CLAMP_MAX_ORDER_VALUE
        if units <= 0:
            # The ceiling already refused an unaffordable lot above, so reaching
            # here means the size itself underflowed: the spec's floor is one lot.
            units = lot if lot is not None else Decimal(1)
            clamped_by = CLAMP_LOT_FLOOR
            reason = f"{reason}; floored to one lot"
        return SizingDecision(
            quantity=Quantity(units),
            source=source,
            reason=reason,
            lots=(units / lot) if lot is not None else units,
            notional=units * price,
            clamped_by=clamped_by,
            stop_distance_ticks=stop_distance_ticks,
            risk_per_lot=risk_per_lot,
            tick_size=tick,
            tick_size_defaulted=tick_defaulted and stop_distance_ticks is not None,
        )

    def _ticks(self, distance: Decimal, tick: Decimal | None) -> Decimal:
        """*distance* in whole ticks, rounded up — a venue stops at ticks."""
        if tick is None:
            return distance
        return (distance / tick).to_integral_value(rounding=ROUND_CEILING)


__all__ = [
    "CASH_EQUITY_ASSET_CLASSES",
    "CASH_EQUITY_LOT_SIZE",
    "CLAMP_LOT_FLOOR",
    "CLAMP_LOT_ROUND",
    "CLAMP_MAX_ORDER_VALUE",
    "DEFAULT_MAX_ORDER_VALUE",
    "SIZING_FALLBACK_TICK",
    "SOURCE_FALLBACK_NOTIONAL",
    "SOURCE_REFUSED",
    "SOURCE_SIGNAL",
    "SOURCE_STOP_DISTANCE",
    "UNCONFIGURED_FALLBACK_FRACTION",
    "ContractSpec",
    "PositionSizer",
    "SizingDecision",
    "account_sizer",
    "unconfigured_sizer",
]
