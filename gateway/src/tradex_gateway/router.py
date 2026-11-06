"""Capability- and health-aware broker selection with failover."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from tradex_domain.errors import (
    AuthenticationError,
    BrokerUnavailableError,
    CapabilityNotSupportedError,
    ConnectionTimeoutError,
    InstrumentNotFoundError,
    RateLimitError,
    SDKError,
)

from tradex_gateway.brokers import BrokerRuntime
from tradex_gateway.config import GatewayConfig
from tradex_gateway.errors import (
    BrokerUnavailable,
    CapabilityUnsupported,
    InstrumentNotFound,
    NoHealthyBroker,
    UnsupportedTimeframe,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

# Ordered policy table: entries are matched in order and the first match wins,
# so the specific SDKError subclasses precede the SDKError catch-all (a rate
# limit keeps its 30 s window instead of collapsing into the generic 5 s). The
# SDKError entry must remain last: any entry added after it would be dead.
_POLICIES: tuple[tuple[tuple[type[BaseException], ...], float], ...] = (
    ((RateLimitError,), 30.0),
    ((ConnectionTimeoutError, BrokerUnavailableError), 15.0),
    ((AuthenticationError,), 60.0),
    ((SDKError,), 5.0),
)

_UNKNOWN_COOLDOWN = 5.0


def _cooldown_for(error: BaseException) -> float | None:
    """Cool-down seconds dictated by the policy table, or ``None`` if unmapped."""
    for kinds, seconds in _POLICIES:
        if isinstance(error, kinds):
            return seconds
    return None


class MarketDataRouter:
    def __init__(
        self,
        config: GatewayConfig,
        runtimes: Mapping[str, BrokerRuntime],
        *,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self._config = config
        self._runtimes = dict(runtimes)
        self._clock = clock
        self._event = on_event or (lambda _name: None)

    def order(self, capability: str, preferred: str | None = None) -> list[BrokerRuntime]:
        names: list[str] = []
        if preferred and preferred in self._runtimes and preferred in self._config.brokers:
            names.append(preferred)
        primary = self._config.primary_for(capability)
        for name in (primary, *self._config.brokers):
            if name not in names:
                names.append(name)
        return [
            self._runtimes[name]
            for name in names
            if name in self._runtimes and self._runtimes[name].available
        ]

    def call(
        self,
        capability: str,
        fn: Callable[[Any], T],
        *,
        preferred: str | None = None,
    ) -> tuple[T, str]:
        ordered = self.order(capability, preferred)
        if not ordered:
            self._event("no_healthy_broker_total")
            raise NoHealthyBroker(f"no broker is configured for {capability}", retry_after=None)
        now = self._clock()
        last: BaseException | None = None
        attempted = False
        for runtime in ordered:
            health = runtime.health
            if not health.available(capability, now):
                continue
            attempted = True
            try:
                result = fn(runtime.broker)
            except (
                ValueError,
                InstrumentNotFoundError,
                CapabilityNotSupportedError,
            ) as exc:
                last = exc
                continue
            except Exception as exc:  # noqa: BLE001 - isolate broker failures
                cooldown = _cooldown_for(exc)
                if cooldown is None:
                    log.exception("unmapped broker failure on %s for %s", runtime.name, capability)
                    cooldown = _UNKNOWN_COOLDOWN
                self._event("broker_failures_total")
                health.record_failure(capability, cooldown, str(exc))
                last = exc
                continue
            health.record_success(capability)
            self._event("broker_success_total")
            return result, runtime.name

        retry_after = self._retry_after(capability, ordered, now)
        if not attempted:
            self._event("no_healthy_broker_total")
            raise NoHealthyBroker(
                f"all {capability} brokers are cooling down", retry_after=retry_after
            )
        if isinstance(last, ValueError):
            raise UnsupportedTimeframe(str(last))
        if isinstance(last, InstrumentNotFoundError):
            raise InstrumentNotFound(str(last))
        if isinstance(last, CapabilityNotSupportedError):
            raise CapabilityUnsupported(str(last))
        if isinstance(
            last,
            (
                RateLimitError,
                AuthenticationError,
                ConnectionTimeoutError,
                BrokerUnavailableError,
            ),
        ):
            raise NoHealthyBroker(
                str(last) if last else f"all {capability} brokers failed",
                retry_after=retry_after,
            ) from last
        raise BrokerUnavailable(
            str(last) if last else f"all {capability} brokers failed",
            retry_after=retry_after,
        ) from last

    def _retry_after(
        self, capability: str, ordered: list[BrokerRuntime], now: float
    ) -> float | None:
        values = [
            runtime.health.retry_after(capability, now)
            for runtime in ordered
            if runtime.health.retry_after(capability, now) > 0
        ]
        return round(min(values), 3) if values else None
