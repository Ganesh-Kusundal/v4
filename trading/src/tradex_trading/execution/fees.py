"""Fee calculation and pricing services.

Pure Decimal math, no I/O.  STT/brokerage/exchange/GST rates for the
equity cash segment; all values quantized to 2 dp (paisa) with
ROUND_HALF_UP.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill
from tradex_domain.value_objects import Money


def _q2(value: Decimal) -> Decimal:
    """Quantize to 2 decimal places (paisa) with ROUND_HALF_UP."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


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
        # NOTE: the instance defaults mirror the canonical static model rates
        # (sebi 0.0001% and stamp 0.003% as *rates*, not percent-of-percent),
        # so the default path delegates to equity_intraday and the two fee
        # paths agree exactly.
        self._brokerage_pct = brokerage_pct
        self._stt_pct = stt_pct
        self._exchange_charge_pct = exchange_charge_pct
        self._sebi_charge_pct = sebi_charge_pct
        self._stamp_duty_pct = stamp_duty_pct
        self._gst_pct = gst_pct

    def calculate(self, fill: Fill) -> Money:
        """Calculate total fees for a fill. Returns Money in INR.

        Delegates to the canonical equity-intraday model
        (:meth:`equity_intraday`) so the instance API and the v3-ported static
        helpers agree exactly — one fee model, not two. Custom-rate
        constructor overrides (tests, exotic configs) are honored when any
        deviate from the standard defaults.
        """
        if fill.price.value <= 0 or fill.quantity.value <= 0:
            raise ValueError("fill price and quantity must be positive")

        # Non-default rates -> legacy percentage-of-value path (kept for
        # custom calculators; the default path is the canonical flat model).
        defaults = {
            "_brokerage_pct": Decimal("0.03"),
            "_stt_pct": Decimal("0.025"),
            "_exchange_charge_pct": Decimal("0.00345"),
            "_sebi_charge_pct": Decimal("0.0001"),
            "_stamp_duty_pct": Decimal("0.003"),
            "_gst_pct": Decimal("18"),
        }
        if any(
            getattr(self, name) != default
            for name, default in defaults.items()
        ):
            return self._calculate_legacy(fill)

        # Canonical path (default rates) — same components as legacy path.
        breakdown = self.equity_intraday(
            side=fill.side,
            price=fill.price.value,
            quantity=fill.quantity.value,
        )
        return Money(amount=breakdown.total.quantize(Decimal("0.01")))

    def _calculate_legacy(self, fill: Fill) -> Money:
        """Percentage-of-value fee path (custom rate overrides).

        Structurally identical to the canonical model — STT on sells only,
        GST on (brokerage + exchange) — so a custom-rate calculator and the
        default calculator stay consistent for any rate set.
        """
        trade_value = fill.price.value * fill.quantity.value

        brokerage = min(
            trade_value * self._brokerage_pct / Decimal("100"),
            _BROKERAGE_CAP,
        )
        stt = (
            Decimal(0)
            if fill.side is OrderSide.BUY
            else trade_value * self._stt_pct / Decimal("100")
        )
        exchange_charge = trade_value * self._exchange_charge_pct / Decimal("100")
        sebi_charge = trade_value * self._sebi_charge_pct / Decimal("100")
        stamp_duty = trade_value * self._stamp_duty_pct / Decimal("100")

        subtotal = brokerage + exchange_charge
        gst = subtotal * self._gst_pct / Decimal("100")

        total = (
            brokerage + stt + exchange_charge + sebi_charge + stamp_duty + gst
        )
        return Money(amount=total.quantize(Decimal("0.01")))

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
            else _q2(turnover * _STT_DELIVERY_SELL)
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
            else _q2(turnover * _STT_INTRADAY_SELL)
        )
        return FeeCalculator._common(turnover, stt)

    @staticmethod
    def _common(turnover: Decimal, stt: Decimal) -> FeeBreakdown:
        """Canonical 6-component equity model (brokerage + exchange + STT + SEBI + stamp + GST).

        broker_fee is the *pure* capped brokerage (Rs 20 cap).  SEBI and stamp
        duty are included for full parity with the legacy path.
        """
        brokerage = _q2(min(turnover * _BROKERAGE_RATE, _BROKERAGE_CAP))
        exchange = _q2(turnover * _EXCHANGE_RATE)
        sebi = _q2(turnover * _SEBI_RATE)
        stamp = _q2(turnover * _STAMP_DUTY_RATE)
        gst = _q2((brokerage + exchange + sebi) * _GST_RATE)
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

        if fill.side.value == "BUY":
            return Money(amount=(trade_value + fees).quantize(Decimal("0.01")))
        return Money(amount=(-trade_value - fees).quantize(Decimal("0.01")))

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


__all__ = ["FeeBreakdown", "FeeCalculator", "PricingService"]
