"""Broker runtimes and per-capability health bookkeeping."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tradex_brokers.common.instruments import instrument_from_id
from tradex_domain.instruments import Instrument
from tradex_domain.value_objects import InstrumentId

from tradex_gateway.config import GatewayConfig

CAPABILITIES = ("live", "quotes", "history", "depth", "chain", "instruments")


@dataclass
class _HealthState:
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    last_error: str | None = None
    last_success: float | None = None


class BrokerHealth:
    """Per-broker, per-capability cooldown state used by the router."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._states: dict[str, _HealthState] = {
            capability: _HealthState() for capability in CAPABILITIES
        }
        self._lock = threading.Lock()

    def state(self, capability: str) -> _HealthState:
        with self._lock:
            return self._states.setdefault(capability, _HealthState())

    def available(self, capability: str, now: float | None = None) -> bool:
        state = self.state(capability)
        return (self._clock() if now is None else now) >= state.cooldown_until

    def retry_after(self, capability: str, now: float | None = None) -> float:
        state = self.state(capability)
        moment = self._clock() if now is None else now
        return max(0.0, state.cooldown_until - moment)

    def record_success(self, capability: str) -> None:
        with self._lock:
            state = self._states.setdefault(capability, _HealthState())
            state.cooldown_until = 0.0
            state.consecutive_failures = 0
            state.last_error = None
            state.last_success = self._clock()

    def record_failure(self, capability: str, seconds: float, message: str) -> None:
        with self._lock:
            state = self._states.setdefault(capability, _HealthState())
            state.consecutive_failures += 1
            state.last_error = message
            state.cooldown_until = max(
                state.cooldown_until, self._clock() + max(0.0, seconds)
            )

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                capability: {
                    "available": self._clock() >= state.cooldown_until,
                    "cooldown_seconds": round(
                        max(0.0, state.cooldown_until - self._clock()), 3
                    ),
                    "consecutive_failures": state.consecutive_failures,
                    "last_error": state.last_error,
                    "last_success": state.last_success,
                }
                for capability, state in self._states.items()
            }


class BrokerRuntime:
    """One broker adapter plus its health and registry helpers."""

    def __init__(
        self,
        name: str,
        broker: Any | None,
        *,
        error: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.broker = broker
        self.error = error
        self.health = BrokerHealth(clock)

    @property
    def available(self) -> bool:
        return self.broker is not None

    @property
    def capabilities(self) -> Any | None:
        return getattr(self.broker, "capabilities", None)

    def instrument(self, instrument_id: InstrumentId) -> Instrument | None:
        if self.broker is None:
            return None
        registry = getattr(self.broker, "registry", None)
        resolved: InstrumentId | None = None
        if registry is not None:
            try:
                resolved = registry.resolve(str(instrument_id))
            except Exception:  # noqa: BLE001 - registry resolution is best effort
                resolved = None
        try:
            return instrument_from_id(resolved or instrument_id)
        except Exception:  # noqa: BLE001 - malformed ID must not crash the gateway
            return None

    def search(self, query: str) -> list[Instrument]:
        if self.broker is None:
            return []
        try:
            return list(self.broker.search(query))
        except Exception:  # noqa: BLE001 - search is best effort
            return []

    def close(self) -> None:
        if self.broker is None:
            return
        try:
            self.broker.close()
        except Exception:  # noqa: BLE001 - shutdown is best effort
            pass


def build_runtimes(
    config: GatewayConfig,
    *,
    builder: Callable[..., Any] | None = None,
) -> dict[str, BrokerRuntime]:
    """Build every configured broker; a failure leaves that broker unavailable."""
    from tradex_runtime.live import build_broker_from_env

    build = builder or build_broker_from_env
    runtimes: dict[str, BrokerRuntime] = {}
    for name in config.brokers:
        try:
            broker = build(
                name,
                allow_order_operations=False,
                load_instruments=True,
                master_cache_dir=config.master_cache_dir,
            )
            broker.connect()
            runtimes[name] = BrokerRuntime(name, broker)
        except Exception as exc:  # noqa: BLE001 - one broker outage must not stop the gateway
            runtimes[name] = BrokerRuntime(
                name, None, error=f"{type(exc).__name__}: {exc}"
            )
    return runtimes
