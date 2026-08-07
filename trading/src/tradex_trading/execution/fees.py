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


@dataclass(frozen=True, slots=True)
class FeeBreakdown:
    """Itemized fee breakdown for a single trade."""

    broker_fee: Decimal
    exchange_fee: Decimal
    stt: Decimal
    gst: Decimal

    @property
    def total(self) -> Decimal:
        """Sum of all fee components."""
        return self.stt + self.broker_fee + self.exchange_fee + self.gst


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
        self._brokerage_pct = brokerage_pct
        self._stt_pct = stt_pct
        self._exchange_charge_pct = exchange_charge_pct
        self._sebi_charge_pct = sebi_charge_pct
        self._stamp_duty_pct = stamp_duty_pct
        self._gst_pct = gst_pct

    def calculate(self, fill: Fill) -> Money:
        """Calculate total fees for a fill. Returns Money in INR."""
        if fill.price.value <= 0 or fill.quantity.value <= 0:
            raise ValueError("fill price and quantity must be positive")
        trade_value = fill.price.value * fill.quantity.value

        brokerage = min(
            trade_value * self._brokerage_pct / Decimal("100"),
            _BROKERAGE_CAP,
        )
        stt = trade_value * self._stt_pct / Decimal("100")
        exchange_charge = trade_value * self._exchange_charge_pct / Decimal("100")
        sebi_charge = trade_value * self._sebi_charge_pct / Decimal("100")
        stamp_duty = trade_value * self._stamp_duty_pct / Decimal("100")

        subtotal = brokerage + exchange_charge + sebi_charge + stamp_duty
        gst = subtotal * self._gst_pct / Decimal("100")

        total = brokerage + stt + exchange_charge + sebi_charge + stamp_duty + gst
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
        brokerage = _q2(min(turnover * _BROKERAGE_RATE, _BROKERAGE_CAP))
        exchange = _q2(turnover * _EXCHANGE_RATE)
        gst = _q2((brokerage + exchange) * _GST_RATE)
        return FeeBreakdown(
            broker_fee=brokerage,
            exchange_fee=exchange,
            stt=stt,
            gst=gst,
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
        return Money(amount=(-trade_value + fees).quantize(Decimal("0.01")))

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
