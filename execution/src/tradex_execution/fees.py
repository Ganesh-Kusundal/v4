# ruff: noqa: E501
"""Fee calculation and pricing services.

Pure Decimal math, no I/O.  Every rate is a fraction of turnover unless the
comment says otherwise, and every component is quantized to 2 dp (paisa) with
ROUND_HALF_UP (``q2``).

**The model is product aware** (AC-6). Each ``ProductType`` with a schedule
resolves to a :class:`FeeSchedule` in :data:`SCHEDULES`, and that schedule —
not a hardcoded call site — decides which components are charged and on which
sides. A product with no schedule is **refused**, not priced: see
:func:`require_fee_schedule`.

Rate tables (NSE equity cash; the datalake and both configured brokers are
NSE, and no BSE table exists yet — see the residual list in the module
constants below):

===============  ==========  ==============  ============  ==================
component        INTRADAY    DELIVERY        both sides?   basis
===============  ==========  ==============  ============  ==================
brokerage        0.03%       0.03%           yes           capped Rs 20/order
STT              0.025%      0.1%            see below     Indian STT Act
exchange txn     0.00345%    0.00345%        yes           NSE cash segment
SEBI turnover    0.0002%     0.0002%         yes           Rs 20 per crore
stamp duty       0.003%      0.003%          yes (both)    conservative
GST              18%         18%             n/a           see below
===============  ==========  ==============  ============  ==================

STT sides, which is the one place the two schedules genuinely differ:

- **Intraday**: 0.025% on the **sell only**.
- **Delivery**: 0.1% on **both** sides. That is the regime in force since
  1 October 2024; before it, delivery STT was sell-side only, which is what
  this model used to charge and which understated a delivery round trip by
  three times the total fee.

GST is 18% on ``brokerage + exchange + SEBI``. The pre-unification legacy
path used a different GST base (tax-bug: brokerage+exchange only, not
brokerage+exchange+sebi) and was removed.

Deliberately **not** modelled, and listed here so the gap is a stated decision
rather than an omission (each needs a dated rate card, not a guess):
DP charges (per scrip per day, so a single fill cannot carry them — they need
a session-level accumulator like the brokerage cap), IPF, the NSE research
fund, NSE clearing charges, and the BSE rate table. The datalake and the
instrument masters are NSE-only today; a BSE-listed symbol would be priced on
the NSE table, which is wrong by the BSE exchange-transaction difference.

SEBI statutory Rs 20/crore (0.0002%) — _SEBI_RATE canonical; constructor
sebi_charge_pct is percent (0.0001 -> 0.0001% = Rs 10/crore, not statutory —
clarified).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from threading import Lock
from typing import TYPE_CHECKING

from tradex_domain.enums import OrderSide, ProductType
from tradex_domain.execution import Fill
from tradex_domain.utils import q2
from tradex_domain.value_objects import Money

if TYPE_CHECKING:
    from tradex_observability.metrics import MetricsRegistry

# ---------------------------------------------------------------------------
# Rate constants
# ---------------------------------------------------------------------------

#: STT as a fraction of turnover. Intraday is sell-side only at 0.025%;
#: delivery is 0.1% on BOTH sides since 1 October 2024.
_STT_INTRADAY_SELL = Decimal("0.00025")
_STT_DELIVERY = Decimal("0.001")
_BROKERAGE_RATE = Decimal("0.0003")  # 0.03%
_BROKERAGE_CAP = Decimal(20)  # Rs 20 per order cap
_EXCHANGE_RATE = Decimal("0.0000345")  # 0.00345% NSE txn charges
_GST_RATE = Decimal("0.18")  # 18% on (brokerage + exchange + sebi)
_SEBI_RATE = Decimal("0.000002")  # Rs 20 per crore
_STAMP_DUTY_RATE = Decimal("0.00003")  # 0.003%

#: The counter the spec names for a fill whose product type was never
#: labelled (``docs/specs/0002-production-readiness-programme``): a null is
#: treated as INTRADAY and counted, so a mislabelled order is visible.
MISSING_PRODUCT_TYPE_METRIC = "fills_missing_product_type_total"


class UnsupportedProductTypeError(ValueError):
    """No fee schedule exists for this product type.

    Raised instead of returning a number. The three products this model
    cannot price yet — ``MARGIN`` (NRML), ``MTF`` and ``COVER_ORDER`` — have
    no rate table, so every candidate answer is wrong: a silent zero says
    margin costs nothing, and the intraday table says margin costs the same
    as a cash intraday trade. A refusal is loud and names the product, so the
    order is refused at submit and nobody is later told a wrong cost.
    """


@dataclass(frozen=True, slots=True)
class FeeBreakdown:
    """Itemized fee breakdown for a single trade."""

    broker_fee: Decimal
    exchange_fee: Decimal
    stt: Decimal
    gst: Decimal
    sebi_fee: Decimal = Decimal("0")
    stamp_duty: Decimal = Decimal("0")

    @property
    def total(self) -> Decimal:
        """Sum of all fee components."""
        components = (
            self.stt, self.broker_fee, self.exchange_fee,
            self.gst, self.sebi_fee, self.stamp_duty,
        )
        return sum(components, start=Decimal("0"))


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """One product's rate table: the rates, and the arithmetic they imply.

    A schedule is the whole cost model for a product, so a component that is
    added later (IPF, research fund, clearing) is a field here rather than a
    branch at a call site. The only difference between the two tables in
    force today is ``stt_buy``.
    """

    product: ProductType
    #: STT as a fraction of turnover, per side. Intraday has no buy-side STT.
    stt_buy: Decimal
    stt_sell: Decimal
    brokerage_rate: Decimal
    #: Per-order brokerage cap in rupees, accrued across partial fills.
    brokerage_cap: Decimal
    exchange_rate: Decimal
    sebi_rate: Decimal
    stamp_duty_rate: Decimal
    gst_rate: Decimal

    def stt_for(self, side: OrderSide) -> Decimal:
        """The STT rate this schedule charges on *side*."""
        return self.stt_buy if side is OrderSide.BUY else self.stt_sell

    def breakdown(
        self, *, side: OrderSide, price: Decimal, quantity: Decimal,
    ) -> FeeBreakdown:
        """Itemized fees for one fill priced from this schedule.

        ``broker_fee`` is the *pure* capped brokerage (Rs 20 cap), because
        :meth:`FeeCalculator.calculate_capped` re-derives GST from whatever
        brokerage headroom the cap left on this particular partial.
        """
        if price <= 0 or quantity <= 0:
            raise ValueError("price and quantity must be positive")
        turnover = price * quantity
        brokerage = q2(min(turnover * self.brokerage_rate, self.brokerage_cap))
        exchange = q2(turnover * self.exchange_rate)
        sebi = q2(turnover * self.sebi_rate)
        stamp = q2(turnover * self.stamp_duty_rate)
        gst = q2((brokerage + exchange + sebi) * self.gst_rate)
        return FeeBreakdown(
            broker_fee=brokerage,
            exchange_fee=exchange,
            stt=q2(turnover * self.stt_for(side)),
            gst=gst,
            sebi_fee=sebi,
            stamp_duty=stamp,
        )


#: NSE equity cash, intraday (MIS). STT is sell-side only.
INTRADAY_SCHEDULE = FeeSchedule(
    product=ProductType.INTRADAY,
    stt_buy=Decimal(0),
    stt_sell=_STT_INTRADAY_SELL,
    brokerage_rate=_BROKERAGE_RATE,
    brokerage_cap=_BROKERAGE_CAP,
    exchange_rate=_EXCHANGE_RATE,
    sebi_rate=_SEBI_RATE,
    stamp_duty_rate=_STAMP_DUTY_RATE,
    gst_rate=_GST_RATE,
)

#: NSE equity cash, delivery (CNC). STT is 0.1% on BOTH sides since
#: 1 October 2024; charging it on the sell only understated a delivery round
#: trip by 175.00 per 1,00,000 of turnover.
DELIVERY_SCHEDULE = FeeSchedule(
    product=ProductType.DELIVERY,
    stt_buy=_STT_DELIVERY,
    stt_sell=_STT_DELIVERY,
    brokerage_rate=_BROKERAGE_RATE,
    brokerage_cap=_BROKERAGE_CAP,
    exchange_rate=_EXCHANGE_RATE,
    sebi_rate=_SEBI_RATE,
    stamp_duty_rate=_STAMP_DUTY_RATE,
    gst_rate=_GST_RATE,
)

#: The priced products. Absence from this table is the definition of "no
#: schedule", and it is a refusal, not a fallback.
SCHEDULES: dict[ProductType, FeeSchedule] = {
    ProductType.INTRADAY: INTRADAY_SCHEDULE,
    ProductType.DELIVERY: DELIVERY_SCHEDULE,
}


def schedule_for(product_type: ProductType) -> FeeSchedule:
    """The schedule for *product_type*, or a refusal naming it.

    This is the one lookup every pricing path goes through, so an unscheduled
    product cannot be reached by a call site that forgot to ask.
    """
    try:
        return SCHEDULES[product_type]
    except KeyError:
        raise UnsupportedProductTypeError(
            f"no fee schedule for product type {product_type.value!r}: "
            f"MARGIN, MTF and COVER_ORDER are refused until a rate table "
            f"exists, rather than charged a silent zero or the "
            f"{ProductType.INTRADAY.value} schedule. Priced products: "
            f"{', '.join(sorted(p.value for p in SCHEDULES))}."
        ) from None


def require_fee_schedule(product_type: ProductType | None) -> ProductType:
    """Resolve *product_type* to a priced product, or refuse.

    The pre-submit door, callable without a :class:`FeeCalculator` instance
    so it works when costs are switched off as well as on. A ``None`` is
    resolved to INTRADAY here and counted by the pricing path, not here: this
    is called once per order at submit, while the counter the spec names
    counts fills.
    """
    if product_type is None:
        return ProductType.INTRADAY
    schedule_for(product_type)
    return product_type


class FeeCalculator:
    """Prices a fill from the schedule its product carries (AC-6).

    The calculator holds no rates of its own: the rate table is
    :data:`SCHEDULES`, keyed by product. A fill whose ``product_type`` is
    null is priced as INTRADAY and counted, because a mislabelled order
    should be visible in a metric rather than silently priced.

    Optional *metrics* must expose ``counter(name)`` — the same duck-typed
    registry the execution engine already takes — and is where
    ``fills_missing_product_type_total`` is published. The count is also kept
    on the instance (:attr:`missing_product_type_count`) so it is readable
    with no registry bound at all.

    The constructor rate parameters predate the schedule table and are still
    accepted for forward compatibility (frozen dataclasses, downstream
    imports), but they are not consulted: the schedule is the only rate
    source, which is what makes one table authoritative.
    """

    def __init__(
        self,
        brokerage_pct: Decimal = Decimal("0.03"),
        stt_pct: Decimal = Decimal("0.025"),
        exchange_charge_pct: Decimal = Decimal("0.00345"),
        sebi_charge_pct: Decimal = Decimal("0.0001"),
        stamp_duty_pct: Decimal = Decimal("0.003"),
        gst_pct: Decimal = Decimal("18"),
        *,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._brokerage_pct = brokerage_pct
        self._stt_pct = stt_pct
        self._exchange_charge_pct = exchange_charge_pct
        self._sebi_charge_pct = sebi_charge_pct
        self._stamp_duty_pct = stamp_duty_pct
        self._gst_pct = gst_pct
        self._metrics = metrics
        self._missing_product_type = 0
        self._count_lock = Lock()

    @property
    def missing_product_type_count(self) -> int:
        """Fills priced from a null ``product_type``, i.e. assumed INTRADAY."""
        return self._missing_product_type

    def _count_missing_product_type(self) -> None:
        with self._count_lock:
            self._missing_product_type += 1
        if self._metrics is not None:
            self._metrics.counter(MISSING_PRODUCT_TYPE_METRIC).inc()

    def _schedule_for_fill(self, fill: Fill) -> FeeSchedule:
        """The schedule *fill* is priced from, or a refusal naming the product.

        A null product type is counted here rather than in
        :func:`require_fee_schedule` because this is the per-fill door and
        the counter the spec names counts fills.
        """
        product = fill.product_type
        if product is None:
            self._count_missing_product_type()
            product = ProductType.INTRADAY
        return schedule_for(product)

    def calculate(self, fill: Fill) -> Money:
        """Total fees for a fill, in INR, from its product's schedule.

        Raises :class:`UnsupportedProductTypeError` for a product with no
        schedule rather than pricing it as intraday or as zero.
        """
        if fill.price.value <= 0 or fill.quantity.value <= 0:
            raise ValueError("fill price and quantity must be positive")

        breakdown = self._schedule_for_fill(fill).breakdown(
            side=fill.side,
            price=fill.price.value,
            quantity=fill.quantity.value,
        )
        return Money(amount=q2(breakdown.total))

    def calculate_capped(
        self,
        fill: Fill,
        accrued_brokerage: Decimal,
    ) -> tuple[Money, Decimal]:
        """Calculate fees with per-order brokerage cap across partial fills.

        Brokerage is capped per order (Rs 20 today, and per schedule rather
        than per call site).  When an order has multiple partial fills, each
        fill's brokerage is limited to the remaining headroom
        ``cap - accrued``.  If the brokerage is capped, GST is recomputed on
        the reduced brokerage so the total stays consistent.

        Parameters
        ----------
        fill : Fill
            The fill to calculate fees for. Its ``product_type`` selects the
            schedule; an unscheduled product is refused here too, because
            this is the door the execution engine actually uses.
        accrued_brokerage : Decimal
            Brokerage already accrued for this order from prior partial fills.

        Returns
        -------
        tuple[Money, Decimal]
            ``(fee, new_accrued)`` — the total fee for this fill and the
            updated accrued brokerage for the order.
        """
        if fill.price.value <= 0 or fill.quantity.value <= 0:
            raise ValueError("fill price and quantity must be positive")

        schedule = self._schedule_for_fill(fill)
        breakdown = schedule.breakdown(
            side=fill.side,
            price=fill.price.value,
            quantity=fill.quantity.value,
        )
        calculated_brokerage = breakdown.broker_fee
        remaining = max(schedule.brokerage_cap - accrued_brokerage, Decimal("0"))
        capped = min(calculated_brokerage, remaining)
        new_accrued = accrued_brokerage + capped

        if capped < calculated_brokerage:
            # Recompute GST on the capped brokerage so total stays consistent.
            gst_new = q2(
                (capped + breakdown.exchange_fee + breakdown.sebi_fee) * schedule.gst_rate
            )
            total = (
                capped
                + breakdown.exchange_fee
                + breakdown.stt
                + breakdown.sebi_fee
                + breakdown.stamp_duty
                + gst_new
            )
            fee = Money(amount=q2(total))
        else:
            fee = Money(amount=q2(breakdown.total))

        return fee, new_accrued

    # -- v3-ported static helpers ------------------------------------------
    # These take an explicit product's rates rather than reading
    # ``Fill.product_type``, and are the same arithmetic the instance paths
    # use — the tables are shared, so the two cannot disagree.

    @staticmethod
    def equity_delivery(
        *,
        side: OrderSide,
        price: Decimal,
        quantity: Decimal,
    ) -> FeeBreakdown:
        """Itemized fees for an equity delivery trade (STT 0.1% both sides)."""
        return DELIVERY_SCHEDULE.breakdown(side=side, price=price, quantity=quantity)

    @staticmethod
    def equity_intraday(
        *,
        side: OrderSide,
        price: Decimal,
        quantity: Decimal,
    ) -> FeeBreakdown:
        """Itemized fees for an equity intraday trade (STT 0.025% sell only)."""
        return INTRADAY_SCHEDULE.breakdown(side=side, price=price, quantity=quantity)

    @staticmethod
    def breakdown_for(
        product_type: ProductType,
        *,
        side: OrderSide,
        price: Decimal,
        quantity: Decimal,
    ) -> FeeBreakdown:
        """Itemized fees for *product_type*, refusing one with no schedule."""
        return schedule_for(product_type).breakdown(
            side=side, price=price, quantity=quantity,
        )
