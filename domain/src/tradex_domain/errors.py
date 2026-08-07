"""Typed v4 error model (FDS 05 §8, decisions D-8).

Every public service raises these stable domain exceptions instead of
broker-native or generic exceptions. ``CapabilityNotSupportedError`` is the
capability-loud failure used when a provider does not implement a feature.
"""

from __future__ import annotations


class SDKError(Exception):
    """Base class for all v4 SDK errors."""


class AuthenticationError(SDKError):
    """Authentication or token refresh failed."""


class RateLimitError(SDKError):
    """Provider rate limit exceeded (local or remote)."""


class BrokerUnavailableError(SDKError):
    """Broker/transport unavailable or unhealthy."""


class ConnectionTimeoutError(BrokerUnavailableError):
    """A network request did not complete within the configured timeout.

    Subclasses :class:`BrokerUnavailableError` so callers that already
    distinguish ``BrokerUnavailableError`` (venue down) from
    :class:`AuthenticationError` (bad credentials) can opt-in to a finer
    timeout distinction without breaking the existing hierarchy.
    """


class OrderRejectedError(SDKError):
    """Order rejected by the provider or risk layer."""


class OrderSubmissionUnknownError(SDKError):
    """The venue may have accepted an order, but its final submission outcome is unknown."""


class InstrumentNotFoundError(SDKError):
    """Instrument could not be resolved."""


class CapabilityNotSupportedError(SDKError):
    """The active provider does not support the requested feature."""


class SessionStateError(SDKError):
    """Operation not allowed in the current session lifecycle state."""


__all__ = [
    "AuthenticationError",
    "BrokerUnavailableError",
    "CapabilityNotSupportedError",
    "ConnectionTimeoutError",
    "InstrumentNotFoundError",
    "OrderRejectedError",
    "OrderSubmissionUnknownError",
    "RateLimitError",
    "SDKError",
    "SessionStateError",
]
