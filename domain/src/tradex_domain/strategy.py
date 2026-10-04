"""Strategy and scanner objects (FDS 05 §6.6/§6.8, decision D-12)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tradex_domain.enums import OrderSide, Timeframe
from tradex_domain.execution import Position
from tradex_domain.instruments import Instrument
from tradex_domain.protocols import SessionFacade
from tradex_domain.serialization import Serializable
from tradex_domain.value_objects import Money, Quantity


@dataclass(frozen=True, slots=True)
class Signal(Serializable):
    instrument: Instrument
    direction: OrderSide
    #: Signal *quality* — how convincing the setup is, for logging, ranking and
    #: selection. **It is not a size and must never be read as one.** It used to
    #: be the order quantity (``abs(signal.strength)`` in the strategy engine
    #: and the backtest bridge), which made every strategy in the repository
    #: trade one share, because every strategy emits ``strength=1.0``. Size is
    #: the sizer's decision; a strategy states intent with ``quantity`` and lets
    #: ``PositionSizer`` turn that into a clamped order size.
    strength: float
    reason: str
    #: Explicit order size in units, or ``None`` to ask the sizer for the
    #: risk-derived size. Nullable because ``None`` is a meaningful instruction
    #: ("size this"), not an error — but a signal must either set it or knowingly
    #: leave it null; nothing downstream may fall back to a default quantity.
    #: Whatever a strategy does set is still clamped by the sizer to whole lots
    #: and to the configured per-order notional.
    quantity: Quantity | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    #: UTC timestamp of the event that produced this signal. ``None`` keeps
    #: hand-built signals backward-compatible; time-aware engines (backtest,
    #: replay) use it to align fills to the correct bar instead of matching
    #: signals to candles sequentially.
    timestamp: datetime | None = None


@dataclass(frozen=True, slots=True)
class StrategyContext(Serializable):
    """Context passed to strategy callbacks."""

    session: SessionFacade | None = None
    instruments: tuple[Instrument, ...] = ()
    positions: tuple[Position, ...] = ()
    position: Position | None = None
    balance: Money | None = None
    timestamp: datetime | None = None
    bar_count: int = 0
    #: Read-only analytics view for the current (instrument, timeframe),
    #: or None when no AnalyticsRuntime is wired. Strategies read indicator
    #: values from this view instead of computing them locally.
    analytics: Any = None


@dataclass(frozen=True, slots=True)
class Condition(Serializable):
    """Scanner condition shape (D-12): no ``Any`` in public contracts."""

    name: str
    params: dict[str, Any] = field(default_factory=dict)
    operator: str = ">"
    threshold: float | int | None = None


@dataclass(frozen=True, slots=True)
class ScannerDefinition(Serializable):
    universe: list[Instrument] = field(default_factory=list)
    conditions: list[Condition] = field(default_factory=list)
    rank_by: str = "score"
    limit: int = 20
    #: Bar timeframe the scanner evaluates on. D1 preserves the
    #: engine's historical daily-scan behaviour; an intraday value
    #: (M5/M15) makes ScannerEngine fetch and evaluate that timeframe.
    timeframe: Timeframe = Timeframe.D1


@dataclass(frozen=True, slots=True)
class ScannerResult(Serializable):
    instrument: Instrument
    score: float
    matched_conditions: list[str]
    indicator_values: dict[str, float]
    rank: int
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = ["Condition", "ScannerDefinition", "ScannerResult", "Signal", "StrategyContext"]
