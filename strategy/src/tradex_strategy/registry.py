"""Strategy registry — manages a collection of StrategyRuntime instances.

``StrategyRegistry`` is the single place that owns all live strategy runtimes.
The ``ReactiveStrategyEngine`` may query it to route events only to RUNNING
strategies; supervising components call ``start_all`` / ``stop_all`` at
session boundaries.
"""

from __future__ import annotations

from tradex_strategy.runtime import StrategyLifecycle, StrategyRuntime

_RUNNING_STATES = frozenset({StrategyLifecycle.RUNNING, StrategyLifecycle.PAUSED})


class StrategyRegistry:
    """Thread-unsafe registry for ``StrategyRuntime`` instances.

    ponytail: single-threaded by design — the trading session's event loop
    owns all mutations; no locking needed until concurrent strategy loading
    is required (upgrade path: wrap mutations in asyncio.Lock).
    """

    def __init__(self) -> None:
        self._runtimes: dict[str, StrategyRuntime] = {}

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def register(self, runtime: StrategyRuntime) -> None:
        """Add a runtime to the registry.

        Raises ``KeyError`` if a runtime with the same strategy_id is already
        registered (duplicate guard — prevents silent overwrites of live
        strategies).
        """
        sid = runtime.artifact.strategy_id
        if sid in self._runtimes:
            raise KeyError(
                f"strategy {sid!r} is already registered; "
                "unregister it first or use a different strategy_id"
            )
        self._runtimes[sid] = runtime

    def unregister(self, strategy_id: str) -> StrategyRuntime | None:
        """Remove and return the runtime for *strategy_id*, or ``None``."""
        return self._runtimes.pop(strategy_id, None)

    def get(self, strategy_id: str) -> StrategyRuntime | None:
        """Return the runtime for *strategy_id*, or ``None``."""
        return self._runtimes.get(strategy_id)

    def list_all(self) -> list[StrategyRuntime]:
        """Return all registered runtimes in registration order."""
        return list(self._runtimes.values())

    def __len__(self) -> int:
        return len(self._runtimes)

    # ------------------------------------------------------------------
    # Bulk lifecycle helpers
    # ------------------------------------------------------------------

    def start_all(self) -> None:
        """Start every CREATED runtime in registration order."""
        for rt in self._runtimes.values():
            if rt.lifecycle is StrategyLifecycle.CREATED:
                rt.start()

    def stop_all(self) -> None:
        """Stop every running/paused runtime in registration order."""
        for rt in self._runtimes.values():
            if rt.lifecycle in _RUNNING_STATES:
                rt.stop()




# ---------------------------------------------------------------------------
# Simple name-based strategy registry (for backtest/replay convenience)
# ---------------------------------------------------------------------------

_SIMPLE_REGISTRY: dict[str, type] = {}


def register_simple(name: str):
    def decorator(cls):
        _SIMPLE_REGISTRY[name] = cls
        return cls
    return decorator


def get_simple(name: str):
    return _SIMPLE_REGISTRY.get(name)


def create_simple(name: str, params: dict | None = None):
    cls = _SIMPLE_REGISTRY.get(name)
    if cls is None:
        raise KeyError(f"Unknown strategy: {name}")
    return cls(params or {})


def list_simple_strategies() -> list[str]:
    return sorted(_SIMPLE_REGISTRY.keys())


def clear_simple() -> None:
    _SIMPLE_REGISTRY.clear()

# Backward-compatible aliases for the simple registry API.
register = register_simple
get = get_simple
create = create_simple
list_strategies = list_simple_strategies
clear = clear_simple
