"""Mode-derived runtime context — the single answer to "what kind of run is this?".

``boot`` used to re-check ``cfg.mode == Mode.LIVE`` (and the paper/live/backtest
variants) at a dozen points: the safety gates, broker construction, the
thread-safe bus, the durable guard, the writer lock. Each check was correct, but
the *meaning* of a mode was scattered and re-derived. ``RuntimeContext`` resolves
the mode once into named predicates and carries the mode-derived ``fill_source``,
so the composition root reads intent (``ctx.is_live``, ``ctx.needs_durable_guard``)
instead of comparing an enum repeatedly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tradex_domain.enums import Mode


@dataclass(slots=True)
class RuntimeContext:
    """The resolved mode plus the collaborator it selects.

    ``fill_source`` is populated by the composition root after the broker exists;
    it is ``None`` until then. Predicates are derived from ``mode`` so they can
    never disagree with it.
    """

    mode: Mode
    fill_source: Any = None

    @property
    def is_live(self) -> bool:
        return self.mode is Mode.LIVE

    @property
    def is_paper(self) -> bool:
        return self.mode is Mode.PAPER

    @property
    def is_backtest(self) -> bool:
        return self.mode is Mode.BACKTEST

    @property
    def is_replay(self) -> bool:
        return self.mode is Mode.REPLAY

    @property
    def is_online(self) -> bool:
        """Paper or live: a run with a venue/broker and a durable book."""
        return self.mode in (Mode.PAPER, Mode.LIVE)

    @property
    def threadsafe_bus(self) -> bool:
        """Live serializes publishes (feed thread + engine threads + API)."""
        return self.mode is Mode.LIVE

    @property
    def needs_durable_guard(self) -> bool:
        """Paper and live always get an idempotency guard (AC-2)."""
        return self.mode in (Mode.PAPER, Mode.LIVE)

    @classmethod
    def from_config(cls, config: Any) -> RuntimeContext:
        """Resolve the context from an ``AppConfig`` (or anything with ``.mode``)."""
        return cls(mode=config.mode)
