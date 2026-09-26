"""Backtest cash ledger — fed by OrderFilled events (orchestrated model).

Pure Decimal accounting used only by BacktestEngine. BUY debits
(price*qty + fee), SELL credits (price*qty - fee). Corporate-action cash
effects (split basis restatement, dividend credit) are applied explicitly by
the backtest orchestrator so the ledger's cash matches the legacy private
loop to the paisa. ``equity(mark)`` adds a caller-supplied mark-to-market of
open positions (BacktestEngine snapshots PositionManager at each bar).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tradex_domain.enums import OrderSide
from tradex_domain.value_objects import Money, Price, Quantity


@dataclass(frozen=True, slots=True)
class CashSnapshot:
    """Immutable cash and fee state exposed by an execution ledger."""

    cash: Decimal
    total_fees: Decimal


class CashLedger:
    """Deterministic cash account for backtest runs."""

    def __init__(
        self,
        initial: Decimal | float | str = Decimal(100000),
        allow_negative: bool = False,
    ) -> None:
        self._cash = Decimal(str(initial))
        self._fees = Decimal(0)
        #: When False (default), any debit that would take cash below zero
        #: raises ValueError. Callers that permit margin/overdraft opt in
        #: with allow_negative=True.
        self._allow_negative = allow_negative

    def _debit(self, amount: Decimal, what: str) -> None:
        """Debit ``amount`` from cash, guarding the buying-power floor."""
        if not self._allow_negative and self._cash - amount < 0:
            raise ValueError(
                f"insufficient buying power: {what} of {amount} would take "
                f"cash from {self._cash} to {self._cash - amount}"
            )
        self._cash -= amount

    @property
    def cash(self) -> Decimal:
        """Current cash balance (trade notional + fees + CA effects)."""
        return self._cash

    @property
    def total_fees(self) -> Decimal:
        """Cumulative fees debited from cash."""
        return self._fees

    def snapshot(self) -> CashSnapshot:
        """Return an immutable snapshot of cash and cumulative fees."""
        return CashSnapshot(cash=self.cash, total_fees=self.total_fees)

    def on_fill(self, side: OrderSide, quantity: Quantity, price: Price) -> None:
        """Apply a fill's notional to cash (debit BUY, credit SELL)."""
        notional = quantity.value * price.value
        if side is OrderSide.BUY:
            self._debit(notional, "BUY fill")
        else:
            self._cash += notional

    def on_fee(self, fee: Decimal | Money) -> None:
        """Debit a fill's fee from cash."""
        amount = fee.amount if isinstance(fee, Money) else Decimal(str(fee))
        self._debit(amount, "fee")
        self._fees += amount

    def credit(self, amount: Decimal) -> None:
        """Explicit cash credit (e.g. dividend per-share * qty)."""
        self._cash += Decimal(str(amount))

    def restate(self, delta: Decimal) -> None:
        """Adjust cash by a corporate-action basis delta (split re-base)."""
        self._cash += Decimal(str(delta))

    def restore(self, cash: Decimal, total_fees: Decimal = Decimal(0)) -> None:
        """Adopt a recovered balance and fee total wholesale.

        Used after a restart, where the authoritative figures come from folding
        the event log rather than from re-applying fills to an empty ledger.
        """
        self._cash = Decimal(str(cash))
        self._fees = Decimal(str(total_fees))

    def equity(self, marked_positions: Decimal) -> Decimal:
        """Total equity = cash + mark-to-market of open positions."""
        return self._cash + Decimal(str(marked_positions))
