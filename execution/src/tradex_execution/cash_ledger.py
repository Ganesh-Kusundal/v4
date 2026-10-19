"""Backtest cash ledger — fed by OrderFilled events (orchestrated model).

Pure Decimal accounting used only by BacktestEngine. BUY debits
(price*qty + fee), SELL credits (price*qty - fee). Corporate-action cash
effects (split basis restatement, dividend credit) are applied explicitly by
the backtest orchestrator so the ledger's cash matches the legacy private
loop to the paisa. ``equity(mark)`` adds a caller-supplied mark-to-market of
open positions (BacktestEngine snapshots PositionAccountant at each bar).

**Concurrency (AC-21).** One ``RLock`` per ledger guards every read-modify-write
of ``_cash``/``_fees``. ``apply_fill`` is the ONLY way a fill moves money: it
holds the lock across the affordability proof, an optional position-booking
callback, the notional debit and the fee debit, so two concurrent submissions
cannot both pass the affordability check and both debit. ``transaction()`` lets
the engine extend that one critical section across its own durable append so the
whole check->persist->book sequence is atomic against other submitters.

**Lock order.** This lock is always taken FIRST, before any other. The position
booking that ``apply_fill`` runs under it then takes the position booker's
per-instrument lock, so the global order is *ledger -> position*. No code path
takes the position lock and then the ledger lock, so the order cannot deadlock.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
    """Deterministic cash account for backtest runs.

    Every mutation of ``_cash``/``_fees`` and every affordability proof is
    serialised by a single per-instance ``RLock`` (AC-21). ``RLock`` rather than
    ``Lock`` because :meth:`apply_fill` re-enters through the public methods
    (:meth:`require_debit`, :meth:`on_fill`, :meth:`on_fee`) and the engine may
    already hold it via :meth:`transaction` -- both are the *same* thread taking
    the same lock again, which an ``RLock`` permits and a plain ``Lock`` would
    deadlock.
    """

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
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Lock scope
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Hold the single ledger lock for a caller-defined critical section.

        The engine wraps its per-door fill sequence in this so the affordability
        proof, the durable append, and the booking/debits all happen with no
        other thread able to observe or race an intermediate state. Re-entrant:
        :meth:`apply_fill` may be called inside and will take the same lock
        again on the same thread without deadlocking.
        """
        with self._lock:
            yield

    # ------------------------------------------------------------------
    # Internal (caller already holds the lock)
    # ------------------------------------------------------------------

    def _debit_unlocked(self, amount: Decimal, what: str) -> None:
        """Debit ``amount`` (caller holds the lock), guarding the floor."""
        if not self._allow_negative and self._cash - amount < 0:
            raise ValueError(
                f"insufficient buying power: {what} of {amount} would take "
                f"cash from {self._cash} to {self._cash - amount}"
            )
        self._cash -= amount

    def _require_debit_unlocked(self, amount: Decimal, what: str) -> None:
        """Raise unless ``amount`` is affordable (caller holds the lock)."""
        if not self._allow_negative and self._cash - amount < 0:
            raise ValueError(
                f"insufficient buying power: {what} of {amount} would take "
                f"cash from {self._cash} to {self._cash - amount}"
            )

    # ------------------------------------------------------------------
    # Public API (each takes the lock; safe to re-enter on one thread)
    # ------------------------------------------------------------------

    def _debit(self, amount: Decimal, what: str) -> None:
        """Debit ``amount`` from cash, guarding the buying-power floor."""
        with self._lock:
            self._debit_unlocked(amount, what)

    def require_debit(self, amount: Decimal, what: str) -> None:
        """Raise unless ``amount`` can be debited, without mutating.

        Lets a caller prove affordability *before* it books anything, so a
        sequence of debits cannot half-apply: the position is already booked
        when the notional debit runs, and a failure there leaves a position
        with no cash movement behind it (M-3). The proof reads the balance under
        the same lock :meth:`apply_fill` holds when it debits, so a proof
        returned here cannot go stale before the matching commit.
        """
        with self._lock:
            self._require_debit_unlocked(amount, what)

    @property
    def cash(self) -> Decimal:
        """Current cash balance (trade notional + fees + CA effects)."""
        with self._lock:
            return self._cash

    @property
    def total_fees(self) -> Decimal:
        """Cumulative fees debited from cash."""
        with self._lock:
            return self._fees

    def snapshot(self) -> CashSnapshot:
        """Return an immutable snapshot of cash and cumulative fees."""
        with self._lock:
            return CashSnapshot(cash=self._cash, total_fees=self._fees)

    def apply_fill(
        self,
        side: OrderSide,
        quantity: Quantity,
        price: Price,
        fee: Decimal | Money,
        what: str,
        book: Callable[[], None] | None = None,
    ) -> None:
        """Apply one fill's whole cash effect atomically, with its booking.

        THE single entry point by which a fill moves money (AC-21). One lock
        spans the ordered steps:

        1. **Affordability proof** -- the notional (BUY only) plus the fee is
           proved against the current balance, exactly as :meth:`require_debit`
           does. This is the step that used to run in a *separate* critical
           section from the debits that follow it, so two concurrent submits
           could both pass and both debit.
        2. **Position booking** -- ``book()`` (the engine passes the position
           booker's fill hook) runs here, still under the ledger lock, so the
           position, cash and fee move as one operation.
        3. **Notional** -- a BUY debits ``price*qty``; a SELL credits it.
        4. **Fee** -- always debited from cash.

        The proof and the debits cannot interleave with another submitter
        because the lock is held across all four steps, so the "check then
        debit" pair is atomic even though steps 3 and 4 are two separate
        arithmetic moves: step 1 proved steps 3 and 4 together fit, and nothing
        runs between them that could change the balance. A proof that fails
        raises before any mutation, so the fill is refused whole and never
        half-applied. ``book()`` is only reached after a successful proof, so a
        refused fill books nothing.

        ``what`` names the debit in the refusal message (the engine passes
        ``"fill <order_id>"``).
        """
        amount = fee.amount if isinstance(fee, Money) else Decimal(str(fee))
        notional = quantity.value * price.value
        debit = (notional if side is OrderSide.BUY else Decimal(0)) + amount
        with self._lock:
            # 1. Prove affordability (no mutation), through the PUBLIC
            #    require_debit so the one named affordability check both doors
            #    and the negative controls exercise is the one that runs here.
            #    A refusal raises now, so book(), the notional and the fee never run.
            self.require_debit(debit, what)
            # 2. Position booking, still under this lock.
            if book is not None:
                book()
            # 3. Notional: BUY debits, SELL credits.
            if side is OrderSide.BUY:
                self._debit_unlocked(notional, what)
            else:
                self._cash += notional
            # 4. Fee always debited (the proof above already covered it).
            self._debit_unlocked(amount, "fee")
            self._fees += amount

    def on_fill(self, side: OrderSide, quantity: Quantity, price: Price) -> None:
        """Apply a fill's notional to cash (debit BUY, credit SELL).

        Retained for recovery's fold and as the notional half of
        :meth:`apply_fill`. Live/paper/backtest fills go through
        :meth:`apply_fill`; this method is not the atomic money path.
        """
        notional = quantity.value * price.value
        with self._lock:
            if side is OrderSide.BUY:
                self._debit_unlocked(notional, "BUY fill")
            else:
                self._cash += notional

    def on_fee(self, fee: Decimal | Money) -> None:
        """Debit a fill's fee from cash (retained for recovery's fold)."""
        amount = fee.amount if isinstance(fee, Money) else Decimal(str(fee))
        with self._lock:
            self._debit_unlocked(amount, "fee")
            self._fees += amount

    def credit(self, amount: Decimal) -> None:
        """Explicit cash credit (e.g. dividend per-share * qty)."""
        with self._lock:
            self._cash += Decimal(str(amount))

    def restate(self, delta: Decimal) -> None:
        """Adjust cash by a corporate-action basis delta (split re-base)."""
        with self._lock:
            self._cash += Decimal(str(delta))

    def restore(self, cash: Decimal, total_fees: Decimal = Decimal(0)) -> None:
        """Adopt a recovered balance and fee total wholesale.

        Used after a restart, where the authoritative figures come from folding
        the event log rather than from re-applying fills to an empty ledger.
        """
        with self._lock:
            self._cash = Decimal(str(cash))
            self._fees = Decimal(str(total_fees))

    def equity(self, marked_positions: Decimal) -> Decimal:
        """Total equity = cash + mark-to-market of open positions."""
        with self._lock:
            return self._cash + Decimal(str(marked_positions))
