"""Backtest cash ledger — fed by OrderFilled events (orchestrated model).

Pure Decimal accounting used only by BacktestEngine. BUY debits
(price*qty + fee), SELL credits (price*qty - fee). Corporate-action cash
effects (split basis restatement, dividend credit) are applied explicitly by
the backtest orchestrator so the ledger's cash matches the legacy private
loop to the paisa. ``equity(mark)`` adds a caller-supplied mark-to-market of
open positions (BacktestEngine snapshots PositionManager at each bar).
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide
from tradex_domain.value_objects import Money, Price, Quantity


class CashLedger:
    """Deterministic cash account for backtest runs."""

    def __init__(self, initial: Decimal | float | str = Decimal("100000")) -> None:
        self._cash = Decimal(str(initial))
        self._fees = Decimal("0")

    @property
    def cash(self) -> Decimal:
        """Current cash balance (trade notional + fees + CA effects)."""
        return self._cash

    @property
    def total_fees(self) -> Decimal:
        """Cumulative fees debited from cash."""
        return self._fees

    def on_fill(self, side: OrderSide, quantity: Quantity, price: Price) -> None:
        """Apply a fill's notional to cash (debit BUY, credit SELL)."""
        notional = quantity.value * price.value
        if side.value == "BUY":
            self._cash -= notional
        else:
            self._cash += notional

    def on_fee(self, fee: Decimal | Money) -> None:
        """Debit a fill's fee from cash."""
        amount = fee.amount if isinstance(fee, Money) else Decimal(str(fee))
        self._cash -= amount
        self._fees += amount

    def credit(self, amount: Decimal) -> None:
        """Explicit cash credit (e.g. dividend per-share * qty)."""
        self._cash += Decimal(str(amount))

    def restate(self, delta: Decimal) -> None:
        """Adjust cash by a corporate-action basis delta (split re-base)."""
        self._cash += Decimal(str(delta))

    def equity(self, marked_positions: Decimal) -> Decimal:
        """Total equity = cash + mark-to-market of open positions."""
        return self._cash + Decimal(str(marked_positions))
