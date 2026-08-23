"""Corporate action store — stores and retrieves corporate actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CorporateAction:
    """Typed corporate action record."""

    instrument: str
    action_type: str  # "DIVIDEND", "SPLIT", "BONUS", "RIGHTS"
    ex_date: str  # ISO date string
    #: Per-share dividend for DIVIDEND actions (shorts pay, so the debit is
    #: per_share * negative qty). Ignored for SPLIT/BONUS.
    amount: float = 0.0
    #: Split/bonus ratio — qty × ratio, avg ÷ ratio (e.g. 2.0 for a 2:1
    #: split or a 1:1 bonus). Ignored for DIVIDEND.
    ratio: float = 1.0


class CorporateActionStore:
    """Stores corporate actions (dividends, splits, etc.).

    Also provides an in-memory split ledger with deterministic price
    adjustment (ported from v3).
    """

    def __init__(self) -> None:
        """Initialize store."""
        self._actions: list[dict] = []
        self._typed_actions: list[CorporateAction] = []
        self._splits: dict[str, float] = {}

    def add(self, action: dict) -> None:
        """Add a corporate action.

        Args:
            action: Dict with keys: instrument, action_type, date, details
        """
        self._actions.append(action)

    def get(self, instrument, start=None, end=None) -> list:
        """Get corporate actions for an instrument.

        Args:
            instrument: Instrument instance
            start: Start datetime (optional)
            end: End datetime (optional)

        Returns:
            List of corporate action dicts
        """
        results = []

        for action in self._actions:
            if action.get('instrument') != instrument:
                continue

            action_date = action.get('date')
            if start and action_date and action_date < start:
                continue
            if end and action_date and action_date > end:
                continue

            results.append(action)

        return results

    # -- typed action API ----------------------------------------------------

    def add_typed(self, action: CorporateAction) -> None:
        """Store a typed corporate action."""
        self._typed_actions.append(action)

    def get_typed(
        self,
        instrument: str,
        action_type: str | None = None,
    ) -> list[CorporateAction]:
        """Return typed actions for *instrument*, optionally filtered by *action_type*."""
        results: list[CorporateAction] = []
        for action in self._typed_actions:
            if action.instrument != instrument:
                continue
            if action_type is not None and action.action_type != action_type:
                continue
            results.append(action)
        return results

    def adjust_series(self, candles: list[Any], symbol: str) -> list[dict[str, Any]]:
        """Return new candle-like dicts with prices adjusted for cumulative splits/bonus.

        Looks up all SPLIT and BONUS typed actions for *symbol* and computes the
        cumulative adjustment ratio.  Each candle's OHLC values are divided by that ratio.
        """
        splits = self.get_typed(symbol, "SPLIT") + self.get_typed(symbol, "BONUS")
        if not splits:
            return [
                {
                    "open": getattr(c, "open", None),
                    "high": getattr(c, "high", None),
                    "low": getattr(c, "low", None),
                    "close": getattr(c, "close", None),
                }
                for c in candles
            ]

        # Cumulative ratio = product of all split ratios
        cumulative_ratio = 1.0
        for split in splits:
            cumulative_ratio *= split.ratio

        adjusted: list[dict[str, Any]] = []
        for c in candles:
            adjusted.append(
                {
                    "open": float(getattr(c, "open", 0)) / cumulative_ratio,
                    "high": float(getattr(c, "high", 0)) / cumulative_ratio,
                    "low": float(getattr(c, "low", 0)) / cumulative_ratio,
                    "close": float(getattr(c, "close", 0)) / cumulative_ratio,
                }
            )
        return adjusted

    # -- split ledger (ported from v3) ----------------------------------------

    def record_split(self, symbol: str, ratio: float) -> None:
        """Record a stock split ratio for *symbol*."""
        if ratio <= 0:
            raise ValueError("split ratio must be positive")
        self._splits[symbol.strip().upper()] = float(ratio)

    def adjust_price(
        self,
        price: float,
        *,
        symbol: str | None = None,
        ratio: float | None = None,
    ) -> float:
        """Return *price* adjusted by a split ratio.

        Pass either an explicit *ratio* or look up a previously recorded
        split via *symbol*.
        """
        if ratio is not None:
            if ratio <= 0:
                raise ValueError("split ratio must be positive")
            return price / ratio
        if symbol is None:
            raise ValueError("either symbol or ratio is required")
        key = symbol.strip().upper()
        if key not in self._splits:
            raise KeyError(symbol)
        return price / self._splits[key]


__all__ = ["CorporateAction", "CorporateActionStore"]
