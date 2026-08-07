"""Multi-strategy ensemble — run multiple strategies on the same data.

Strategies can be combined with different allocation weights to produce
composite signals. Supports equal-weight, custom-weight, and priority-based
signal aggregation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from tradex_domain import Candle, Quote, Signal
from tradex_domain.enums import OrderSide
from tradex_domain.strategy import StrategyContext


@dataclass(frozen=True, slots=True)
class StrategyEntry:
    """A strategy with its allocation weight."""
    strategy: Any
    weight: float = 1.0
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", type(self.strategy).__name__)


class StrategyEnsemble:
    """Run multiple strategies and aggregate their signals.

    Example
    -------
    ```python
    ensemble = StrategyEnsemble([
        StrategyEntry(ma_cross, weight=0.5, name="MA Crossover"),
        StrategyEntry(rsi_strat, weight=0.3, name="RSI"),
        StrategyEntry(mom_strat, weight=0.2, name="Momentum"),
    ])

    # Run all strategies on a bar
    signals = ensemble.on_bar(context, candle)

    # Get aggregated signal
    composite = ensemble.aggregate(signals)
    ```
    """

    def __init__(
        self,
        entries: Sequence[StrategyEntry] | None = None,
        *,
        aggregation: str = "weighted",
        min_votes: int = 1,
    ) -> None:
        """Initialize ensemble.

        Parameters
        ----------
        entries : Sequence[StrategyEntry] | None
            Strategies with weights.
        aggregation : str
            Aggregation method: "weighted", "majority", "priority".
        min_votes : int
            Minimum strategies that must agree (for "majority").
        """
        self._entries = list(entries or [])
        self._aggregation = aggregation
        self._min_votes = min_votes

    def add(
        self,
        strategy: Any,
        weight: float = 1.0,
        name: str = "",
    ) -> StrategyEnsemble:
        """Add a strategy to the ensemble.

        Returns self for chaining.
        """
        self._entries.append(StrategyEntry(strategy=strategy, weight=weight, name=name))
        return self

    def remove(self, name: str) -> StrategyEnsemble:
        """Remove a strategy by name.

        Returns self for chaining.
        """
        self._entries = [e for e in self._entries if e.name != name]
        return self

    @property
    def strategies(self) -> list[StrategyEntry]:
        """Return all strategy entries."""
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def on_start(self, context: StrategyContext) -> None:
        """Notify all strategies of start."""
        for entry in self._entries:
            if hasattr(entry.strategy, "on_start"):
                entry.strategy.on_start(context)

    def on_stop(self, context: StrategyContext) -> None:
        """Notify all strategies of stop."""
        for entry in self._entries:
            if hasattr(entry.strategy, "on_stop"):
                entry.strategy.on_stop(context)

    def on_bar(self, context: StrategyContext, bar: Candle) -> list[tuple[str, Signal | None]]:
        """Run all strategies on a bar.

        Returns list of (strategy_name, signal_or_none) tuples.
        """
        results = []
        for entry in self._entries:
            signal = None
            if hasattr(entry.strategy, "on_bar"):
                try:
                    signal = entry.strategy.on_bar(context, bar)
                except Exception:
                    pass
            results.append((entry.name, signal))
        return results

    def on_quote(self, context: StrategyContext, quote: Quote) -> list[tuple[str, Signal | None]]:
        """Run all strategies on a quote.

        Returns list of (strategy_name, signal_or_none) tuples.
        """
        results = []
        for entry in self._entries:
            signal = None
            if hasattr(entry.strategy, "on_quote"):
                try:
                    signal = entry.strategy.on_quote(context, quote)
                except Exception:
                    pass
            results.append((entry.name, signal))
        return results

    def aggregate(
        self,
        results: list[tuple[str, Signal | None]],
    ) -> Signal | None:
        """Aggregate signals from multiple strategies.

        Parameters
        ----------
        results : list[tuple[str, Signal | None]]
            Output from on_bar() or on_quote().

        Returns
        -------
        Signal | None
            Aggregated signal, or None if no consensus.
        """
        active = [(name, sig) for name, sig in results if sig is not None]
        if not active:
            return None

        if self._aggregation == "majority":
            return self._aggregate_majority(active)
        elif self._aggregation == "priority":
            return self._aggregate_priority(active)
        else:
            return self._aggregate_weighted(active)

    def _aggregate_weighted(
        self,
        active: list[tuple[str, Signal]],
    ) -> Signal | None:
        """Weighted average of signal strengths."""
        buy_score = 0.0
        sell_score = 0.0
        total_weight = 0.0

        for name, sig in active:
            entry = next((e for e in self._entries if e.name == name), None)
            w = entry.weight if entry else 1.0
            strength = getattr(sig, "strength", 1.0) or 1.0
            side = getattr(sig, "direction", None) or getattr(sig, "side", None)

            if side == OrderSide.BUY:
                buy_score += w * strength
            elif side == OrderSide.SELL:
                sell_score += w * strength
            total_weight += w

        if buy_score > sell_score and buy_score > 0:
            ref_sig = active[0][1]
            return Signal(
                instrument=ref_sig.instrument,
                direction=OrderSide.BUY,
                strength=min(buy_score / total_weight, 1.0) if total_weight else 0,
                reason=f"ensemble_weighted({len(active)} strategies)",
            )
        elif sell_score > buy_score and sell_score > 0:
            ref_sig = active[0][1]
            return Signal(
                instrument=ref_sig.instrument,
                direction=OrderSide.SELL,
                strength=min(sell_score / total_weight, 1.0) if total_weight else 0,
                reason=f"ensemble_weighted({len(active)} strategies)",
            )
        return None

    def _aggregate_majority(
        self,
        active: list[tuple[str, Signal]],
    ) -> Signal | None:
        """Majority vote."""
        buy_count = 0
        sell_count = 0

        for _, sig in active:
            side = getattr(sig, "direction", None) or getattr(sig, "side", None)
            if side == OrderSide.BUY:
                buy_count += 1
            elif side == OrderSide.SELL:
                sell_count += 1

        if buy_count >= self._min_votes and buy_count > sell_count:
            ref_sig = active[0][1]
            return Signal(
                instrument=ref_sig.instrument,
                direction=OrderSide.BUY,
                strength=buy_count / len(active),
                reason=f"ensemble_majority({buy_count}/{len(active)})",
            )
        elif sell_count >= self._min_votes and sell_count > buy_count:
            ref_sig = active[0][1]
            return Signal(
                instrument=ref_sig.instrument,
                direction=OrderSide.SELL,
                strength=sell_count / len(active),
                reason=f"ensemble_majority({sell_count}/{len(active)})",
            )
        return None

    def _aggregate_priority(
        self,
        active: list[tuple[str, Signal]],
    ) -> Signal | None:
        """Return signal from highest-priority (first added) strategy."""
        if active:
            return active[0][1]
        return None


__all__ = ["StrategyEnsemble", "StrategyEntry"]
