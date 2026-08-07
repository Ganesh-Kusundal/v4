"""Health check utilities for the trading session.

Provides health status reporting for monitoring and diagnostics.
Includes component-level health checks with aggregate worst-state reporting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from tradex_trading.sdk.session import TradingSession

_SEVERITY = {
    "ERROR": 4,
    "STOPPING": 3,
    "DEGRADED": 2,
    "STARTING": 1,
    "INITIALIZING": 1,
    "RUNNING": 0,
    "UNKNOWN": 0,
}


# ---------------------------------------------------------------------------
# Component-level health protocol (ported from v3)
# ---------------------------------------------------------------------------


class ComponentState(StrEnum):
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"
    INITIALIZING = "INITIALIZING"
    STARTING = "STARTING"
    STOPPING = "STOPPING"


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    component_id: str
    state: ComponentState = ComponentState.RUNNING
    details: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class HealthCheck(Protocol):
    def check(self) -> ComponentHealth: ...


class MessageBusHealthCheck:
    """Check whether the message bus is still accepting events."""

    def __init__(self, bus: Any) -> None:
        self._bus = bus

    def check(self) -> ComponentHealth:
        stopped = bool(getattr(self._bus, "stopped", False))
        return ComponentHealth(
            component_id="message_bus",
            state=ComponentState.ERROR if stopped else ComponentState.RUNNING,
            details={"stopped": stopped},
        )


class CacheHealthCheck:
    """Report order/position counts from the trading cache."""

    def __init__(self, cache: Any) -> None:
        self._cache = cache

    def check(self) -> ComponentHealth:
        snapshot = self._cache.snapshot() if hasattr(self._cache, "snapshot") else {}
        return ComponentHealth(
            component_id="trading_cache",
            state=ComponentState.RUNNING,
            details={
                "orders": len(snapshot.get("orders", {})),
                "positions": len(snapshot.get("positions", {})),
            },
        )


class ClockHealthCheck:
    """Verify the injectable clock is still functional."""

    def __init__(self, clock: Any) -> None:
        self._clock = clock

    def check(self) -> ComponentHealth:
        try:
            self._clock.now()
        except Exception as exc:  # noqa: BLE001 — any clock failure flips to ERROR
            return ComponentHealth(
                component_id="clock",
                state=ComponentState.ERROR,
                details={"error": str(exc)},
            )
        return ComponentHealth(component_id="clock", state=ComponentState.RUNNING, details={})


class AggregateHealthCheck:
    """Overall state is the worst of the individual results."""

    def __init__(self, checks: list[HealthCheck]) -> None:
        self._checks = checks

    def check(self) -> ComponentHealth:
        results = [c.check() for c in self._checks]
        worst = max(results, key=lambda r: _SEVERITY[r.state.value], default=None)
        if worst is None:
            return ComponentHealth(
                component_id="aggregate", state=ComponentState.RUNNING, details={}
            )
        return ComponentHealth(
            component_id="aggregate",
            state=worst.state,
            details={r.component_id: r.state.value for r in results},
        )

    def is_ready(self) -> bool:
        return self.check().state is ComponentState.RUNNING


# ---------------------------------------------------------------------------
# Session-level health (v4 original)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """Health status report.

    Attributes
    ----------
    status : str
        Overall status: "ok", "degraded", "unhealthy".
    broker : str
        Broker identifier.
    mode : str
        Execution mode.
    details : dict
        Additional diagnostic information.
    """

    status: str
    broker: str
    mode: str
    details: dict = field(default_factory=dict)


def check_health(session: TradingSession) -> HealthStatus:
    """Check the health of a trading session.

    Parameters
    ----------
    session : TradingSession
        The session to check.

    Returns
    -------
    HealthStatus
        Health status report.
    """
    details: dict[str, Any] = {}

    # Check session state
    if session.state.value != "READY":
        return HealthStatus(
            status="unhealthy",
            broker=session.broker_id.value,
            mode="unknown",
            details={"error": f"Session in {session.state} state"},
        )

    # Check broker connectivity
    try:
        broker = session._broker
        if hasattr(broker, "get_account"):
            account = broker.get_account()
            if account.balance is not None:
                details["account_balance"] = str(account.balance.amount)
    except Exception as e:
        return HealthStatus(
            status="unhealthy",
            broker=session.broker_id.value,
            mode="unknown",
            details={"error": f"Broker check failed: {e}"},
        )

    # Check bus
    try:
        _ = session.bus
        details["bus_active"] = True
    except Exception as e:
        return HealthStatus(
            status="degraded",
            broker=session.broker_id.value,
            mode="unknown",
            details={"warning": f"Bus check failed: {e}"},
        )

    return HealthStatus(
        status="ok",
        broker=session.broker_id.value,
        mode=session.mode,
        details=details,
    )


__all__ = [
    "AggregateHealthCheck",
    "CacheHealthCheck",
    "ClockHealthCheck",
    "ComponentHealth",
    "ComponentState",
    "HealthCheck",
    "HealthStatus",
    "MessageBusHealthCheck",
    "check_health",
]
