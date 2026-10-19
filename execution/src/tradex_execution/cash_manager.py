"""Cash ledger management — extracted from ExecutionEngine.

Owns the cash ledger, the anchor flag, and the restore/snapshot operations.
The engine delegates to this; it does not touch the ledger directly.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tradex_domain.events import CashAccountInitialized

from tradex_execution.cash_ledger import CashLedger, CashSnapshot


class CashManager:
    """Owns the cash ledger and its anchor state.

    Parameters
    ----------
    append:
        Callable that publishes a domain event to the bus. Used to write the
        opening ``CashAccountInitialized`` anchor.
    allow_negative:
        The engine's cash floor policy, applied to any ledger born here.
    """

    def __init__(self, append: Any, allow_negative: bool = False) -> None:
        self._append = append
        self._allow_negative = allow_negative
        self._ledger: CashLedger | None = None
        self._anchor_appended = False

    def restore(
        self,
        cash: Decimal,
        total_fees: Decimal = Decimal(0),
        *,
        re_anchor: bool = False,
    ) -> None:
        """Set the cash ledger's balance to an absolute figure.

        A restarted process starts with an empty ledger, so without this it
        reports ``0`` and the fail-closed cash gate denies every BUY against a
        balance the process actually holds.

        **This is a setter, and it is a setter because of who calls it.** Every
        production caller passes a balance it is authoritative about, and
        adopts it whole:

        - ``startup.py`` cold boot, ``re_anchor=True`` — the opening balance
          read from broker funds. A **seed**.
        - ``startup.py`` recovery — the balance and fee total folded out of the
          event log. A **fold**, adopted whole, re-deriving nothing.
        - ``BacktestEngine``'s corporate-action path, which reads
          ``cash_snapshot``, adds the basis delta, and writes the sum back. A
          **setter used as a delta by its caller** — the delta is the caller's
          arithmetic, not this method's argument.
        - the engine constructor, re-anchoring a ledger it already holds.

        So it is neither cleanly a seed nor cleanly a delta, and the difference
        decides the fix. It must **not** be a debit: an opening balance is not
        something an account has to be able to afford, and implementing the
        seed as one lands the balance at ``-cash`` and refuses every legitimate
        boot. The floor belongs to the ledger's *policy*, and the only thing to
        decide is which policy a ledger born here is given.

        It is the engine's configured ``allow_negative_cash``, the same value
        the constructor applies — which is what makes the live and paper paths
        enforce the same floor the backtest enforces (AC-2). Creating that
        ledger with ``allow_negative=True`` instead made ``require_debit`` and
        ``_debit`` no-ops, so in live and paper every pre-check passed and a
        fill the account could not pay for booked its position and drove cash
        below zero with nothing raised, refused or published.

        *re_anchor* appends a fresh ``CashAccountInitialized``. That is only
        correct for a genuinely NEW opening balance (a cold boot reading broker
        funds). A restart restoring a fold must leave it False: the anchor is
        already in the log, and appending the folded balance again would both
        grow the log and make the next fold re-apply the fills on top of a
        balance that already includes them.
        """
        if self._ledger is None:
            self._ledger = CashLedger(
                initial=cash, allow_negative=self._allow_negative,
            )
        else:
            self._ledger.restore(cash, total_fees)
        if re_anchor:
            if not self._anchor_appended:
                # Only a *successful* write proves the log carries an anchor. If
                # this fails, the flag stays False and a later boot knows the
                # account is still unanchored instead of trusting a phantom one.
                self._anchor_appended = self._append(
                    CashAccountInitialized(amount=cash),
                )
                return
        # A restore proves an anchor already exists in the log, so record that
        # even when this call did not write one. Without it a restarted process
        # would still look like a cold boot and anchor a second time.
        self._anchor_appended = True

    def reset_anchor(self) -> None:
        """Clear the anchor flag so the next restore re-anchors."""
        self._anchor_appended = False

    @property
    def balance_ledger(self) -> CashLedger | None:
        """The balance ledger this manager owns (None before first restore).

        Public delegate for the engine's affordability/fee/fill reads — the
        engine reaches the balance only through its owner, never by holding a
        second ledger of its own.
        """
        return self._ledger

    @property
    def anchor_missing(self) -> bool:
        """True when no opening anchor has been written for this ledger.

        Startup uses it to tell a cold boot (a new opening balance must be
        anchored) from a restart (the log already has one).
        """
        return not self._anchor_appended

    def snapshot(self) -> CashSnapshot:
        """Return reactive cash state, or a disabled zero snapshot."""
        if self._ledger is None:
            return CashSnapshot(cash=Decimal(0), total_fees=Decimal(0))
        return self._ledger.snapshot()
