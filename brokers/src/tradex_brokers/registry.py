"""BrokerFactory plugin registry.

Allows broker adapters to register themselves by ``BrokerId`` so that the
rest of the platform can instantiate them without hard-coding imports.
"""

from __future__ import annotations

import logging
from typing import Any

from tradex_domain import BrokerAdapter, BrokerId, BrokerUnavailableError

log = logging.getLogger(__name__)


class BrokerFactory:
    """Registry mapping ``BrokerId`` → adapter class.

    Broker adapters call ``register()`` at import time (or via a plugin
    entry-point) to make themselves available.  The rest of the platform
    calls ``create()`` to instantiate an adapter by its ``BrokerId``.
    """

    _registry: dict[BrokerId, type] = {}

    @classmethod
    def register(cls, broker_id: BrokerId, adapter_class: type) -> None:
        """Register an adapter class for a broker.

        Parameters
        ----------
        broker_id:
            The broker identifier to register under.
        adapter_class:
            The adapter class to instantiate when ``create()`` is called.

        Raises
        ------
        TypeError
            If *broker_id* is not a ``BrokerId`` or *adapter_class* is not a
            class.
        """
        if not isinstance(broker_id, BrokerId):
            raise TypeError(
                f"broker_id must be a BrokerId enum member, got {type(broker_id).__name__}"
            )
        if not isinstance(adapter_class, type):
            raise TypeError(
                f"adapter_class must be a class, got {type(adapter_class).__name__}"
            )
        # Structural protocol check: verify the class instance conforms to
        # ``BrokerAdapter`` before registering, so a broken adapter fails at
        # registration time (import-time for the builtin brokers) rather
        # than at first ``create()``.
        if not isinstance(adapter_class(), BrokerAdapter):  # type: ignore[call-arg]
            raise TypeError(
                f"adapter_class {adapter_class.__name__} does not conform to "
                "the BrokerAdapter protocol"
            )
        if broker_id in cls._registry:
            log.warning(
                "Overwriting existing registration for %s (%s → %s)",
                broker_id.value,
                cls._registry[broker_id].__name__,
                adapter_class.__name__,
            )
        cls._registry[broker_id] = adapter_class
        log.debug("Registered broker adapter %s → %s", broker_id.value, adapter_class.__name__)

    @classmethod
    def create(cls, broker_id: BrokerId, **kwargs: Any) -> Any:
        """Instantiate a registered broker adapter.

        Parameters
        ----------
        broker_id:
            The broker to instantiate.
        **kwargs:
            Forwarded to the adapter's constructor.

        Returns
        -------
        Any
            An instance of the registered adapter class.

        Raises
        ------
        BrokerUnavailableError
            If no adapter is registered for *broker_id*.
        """
        adapter_class = cls._registry.get(broker_id)
        if adapter_class is None:
            available = ", ".join(b.value for b in cls._registry) or "(none)"
            raise BrokerUnavailableError(
                f"No adapter registered for broker {broker_id.value}. "
                f"Available: {available}"
            )
        return adapter_class(**kwargs)

    @classmethod
    def available(cls) -> list[BrokerId]:
        """Return a sorted list of registered broker IDs."""
        return sorted(cls._registry.keys(), key=lambda b: b.value)

    @classmethod
    def is_registered(cls, broker_id: BrokerId) -> bool:
        """Return ``True`` if an adapter is registered for *broker_id*."""
        return broker_id in cls._registry

    @classmethod
    def clear(cls) -> None:
        """Remove all registrations (primarily for testing)."""
        cls._registry.clear()


__all__ = [
    "BrokerFactory",
]
