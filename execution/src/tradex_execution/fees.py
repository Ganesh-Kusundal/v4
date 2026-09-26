# ruff: noqa: E501
"""Fee calculation and pricing services.

Pure Decimal math, no I/O.  STT/brokerage/exchange/GST rates for the
equity cash segment; all values quantized to 2 dp (paisa) with
ROUND_HALF_UP.

One fee model: the canonical equity-intraday breakdown in
:meth:`FeeCalculator.equity_intraday` is the only path used by
:meth:`FeeCalculator.calculate` (M1 unification, 2026-08-29).  The
pre-unification legacy path used a different GST base (tax-bug: GST
on brokerage+exchange only, not brokerage+exchange+sebi) and was
removed.

SEBI statutory ₹20/crore (0.0002%) — _SEBI_RATE canonical; constructor sebi_charge_pct is percent (0.0001 → 0.0001% = ₹10/crore, not statutory — clarified).

Delivery STT pre-Oct-2024 sell-only; post-Oct-2024 both sides — current equity_delivery models conservative pre-reform.

Stamp duty equity buy-side only — we charge both sides (conservative).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill
from tradex_domain.utils import q2
from tradex_domain.value_objects import Money

# ---------------------------------------------------------------------------
# Fee breakdown
# ---------------------------------------------------------------------------

_STT_DELIVERY_SELL = Decimal("0.001")  # 0.1% on delivery sell only
_STT_INTRADAY_SELL = Decimal("0.00025")  # 0.025% on intraday sell only
_BROKERAGE_RATE = Decimal("0.0003")  # 0.03%
_BROKERAGE_CAP = Decimal(20)  # Rs 20 per order cap
_EXCHANGE_RATE = Decimal("0.0000345")  # 0.00345% NSE txn charges
_GST_RATE = Decimal("0.18")  # 18% on (brokerage + exchange)
_SEBI_RATE = Decimal("0.000002")  # ₹20 per crore
_STAMP_DUTY_RATE = Decimal("0.00003")  # 0.003%


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


class FeeCalculator:
    """Calculates trading fees (STT, exchange charges, brokerage, etc.).

    Defaults are tuned for Indian equity intraday (zero-brokerage model).

    SEBI statutory ₹20/crore (0.0002%) — _SEBI_RATE canonical; constructor sebi_charge_pct is percent (0.0001 → 0.0001% = ₹10/crore, not statutory — clarified).
    """

    def __init__(
        self,
        brokerage_pct: Decimal = Decimal("0.03"),
        stt_pct: Decimal = Decimal("0.025"),
        exchange_charge_pct: Decimal = Decimal("0.00345"),
        sebi_charge_pct: Decimal = Decimal("0.0001"),
        stamp_duty_pct: Decimal = Decimal("0.003"),
        gst_pct: Decimal = Decimal("18"),
    ) -> None:
        # The instance defaults mirror the canonical static model rates and
        # are accepted on the constructor for forward compatibility (frozen
        # dataclasses, downstream imports); the canonical ``equity_intraday``
        # path uses the static constants directly, so the constructor
        # parameters are accepted but not consulted.
        self._brokerage_pct = brokerage_pct
        self._stt_pct = stt_pct
        self._exchange_charge_pct = exchange_charge_pct
        self._sebi_charge_pct = sebi_charge_pct
        self._stamp_duty_pct = stamp_duty_pct
        self._gst_pct = gst_pct

    def calculate(self, fill: Fill) -> Money:
        """Calculate total fees for a fill. Returns Money in INR.

        One fee model, one path: the canonical equity-intraday breakdown
        (brokerage + exchange + sebi + stamp + STT-on-sell + 18% GST on
        brokerage+exchange+sebi).  The pre-unification legacy path used
        a different GST base (tax-bug: GST on brokerage+exchange only,
        not brokerage+exchange+sebi) and was removed in M1.

        Constructor rate parameters are kept for forward compatibility
        (frozen dataclasses, downstream imports) but are not consulted
        by ``calculate`` — the canonical static-rate path is the only
        path, so instance API and the v3-ported static helpers agree
        exactly.
        """
        if fill.price.value <= 0 or fill.quantity.value <= 0:
            raise ValueError("fill price and quantity must be positive")

        breakdown = self.equity_intraday(
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

        Brokerage is capped at Rs 20 per order.  When an order has multiple
        partial fills, each fill's brokerage is limited to the remaining
        headroom ``20 - accrued``.  If the brokerage is capped, GST is
        recomputed on the reduced brokerage so the total stays consistent.

        Parameters
        ----------
        fill : Fill
            The fill to calculate fees for.
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

        breakdown = self.equity_intraday(
            side=fill.side,
            price=fill.price.value,
            quantity=fill.quantity.value,
        )
        calculated_brokerage = breakdown.broker_fee
        remaining = max(_BROKERAGE_CAP - accrued_brokerage, Decimal("0"))
        capped = min(calculated_brokerage, remaining)
        new_accrued = accrued_brokerage + capped

        if capped < calculated_brokerage:
            # Recompute GST on the capped brokerage so total stays consistent.
            gst_new = q2(
                (capped + breakdown.exchange_fee + breakdown.sebi_fee) * _GST_RATE
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

    @staticmethod
    def equity_delivery(
        *,
        side: OrderSide,
        price: Decimal,
        quantity: Decimal,
    ) -> FeeBreakdown:
        """Calculate itemized fees for an equity delivery trade."""
        if price <= 0 or quantity <= 0:
            raise ValueError("price and quantity must be positive")
        turnover = price * quantity
        stt = (
            Decimal(0)
            if side is OrderSide.BUY
            else q2(turnover * _STT_DELIVERY_SELL)
        )
        return FeeCalculator._common(turnover, stt)

    @staticmethod
    def equity_intraday(
        *,
        side: OrderSide,
        price: Decimal,
        quantity: Decimal,
    ) -> FeeBreakdown:
        """Calculate itemized fees for an equity intraday trade."""
        if price <= 0 or quantity <= 0:
            raise ValueError("price and quantity must be positive")
        turnover = price * quantity
        stt = (
            Decimal(0)
            if side is OrderSide.BUY
            else q2(turnover * _STT_INTRADAY_SELL)
        )
        return FeeCalculator._common(turnover, stt)

    @staticmethod
    def _common(turnover: Decimal, stt: Decimal) -> FeeBreakdown:
        """Canonical 6-component equity model (brokerage + exchange + STT + SEBI + stamp + GST).

        broker_fee is the *pure* capped brokerage (Rs 20 cap).  SEBI and stamp
        duty are included for full parity with the legacy path.
        """
        brokerage = q2(min(turnover * _BROKERAGE_RATE, _BROKERAGE_CAP))
        exchange = q2(turnover * _EXCHANGE_RATE)
        sebi = q2(turnover * _SEBI_RATE)
        stamp = q2(turnover * _STAMP_DUTY_RATE)
        gst = q2((brokerage + exchange + sebi) * _GST_RATE)
        return FeeBreakdown(
            broker_fee=brokerage,
            exchange_fee=exchange,
            stt=stt,
            gst=gst,
            sebi_fee=sebi,
            stamp_duty=stamp,
        )


class PricingService:
    """Combines fee calculation with order pricing."""

    def __init__(self, fee_calculator: FeeCalculator | None = None) -> None:
        self._fees = fee_calculator or FeeCalculator()

    def total_cost(self, fill: Fill) -> Money:
        """Return total cost of a fill including fees.

        For a BUY, cost is positive (money spent).
        For a SELL, cost is negative (money received) minus fees.
        """
        trade_value = fill.price.value * fill.quantity.value
        fees = self._fees.calculate(fill).amount

        if fill.side is OrderSide.BUY:
            return Money(amount=q2(trade_value + fees))
        return Money(amount=q2(-trade_value - fees))

    # -- v3-ported static helpers ------------------------------------------

    @staticmethod
    def vwap(prices: list[Decimal], quantities: list[Decimal]) -> Decimal:
        """Calculate volume-weighted average price.

        Raises ValueError when inputs are empty or differ in length.
        """
        if len(prices) != len(quantities) or not prices:
            raise ValueError(
                "prices and quantities must be non-empty and same length",
            )
        total_value = sum(
            (p * q for p, q in zip(prices, quantities, strict=True)),
            Decimal(0),
        )
        total_qty = sum(quantities, Decimal(0))
        if total_qty == 0:
            raise ValueError("total quantity must not be zero")
        return total_value / total_qty

    @staticmethod
    def slippage_bps(
        expected_price: Decimal,
        fill_price: Decimal,
    ) -> Decimal:
        """Return slippage in basis points (rounded to whole bps)."""
        if expected_price == Decimal(0):
            raise ValueError("expected_price must not be zero")
        diff = fill_price - expected_price
        return (diff / expected_price * Decimal(10000)).quantize(Decimal(1))


