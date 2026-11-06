"""Typed gateway errors and the wire envelope."""

from __future__ import annotations

from typing import Any


class GatewayError(Exception):
    status = 500
    code = "internal_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        broker: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.broker = broker
        self.retry_after = retry_after

    def to_envelope(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "broker": self.broker,
                "retryable": self.retryable,
                "retry_after": self.retry_after,
            }
        }


class Unauthorized(GatewayError):
    status = 401
    code = "unauthorized"


class Forbidden(GatewayError):
    status = 403
    code = "forbidden"


class BadRequest(GatewayError):
    status = 400
    code = "invalid_request"


class InstrumentNotFound(GatewayError):
    status = 404
    code = "instrument_not_found"


class UnsupportedTimeframe(GatewayError):
    status = 422
    code = "unsupported_timeframe"


class CapabilityUnsupported(GatewayError):
    status = 422
    code = "capability_not_supported"


class ConsumerRateLimited(GatewayError):
    status = 429
    code = "consumer_rate_limited"
    retryable = True


class BrokerRateLimited(GatewayError):
    status = 503
    code = "broker_rate_limited"
    retryable = True


class BrokerUnavailable(GatewayError):
    status = 503
    code = "broker_unavailable"
    retryable = True


class NoHealthyBroker(GatewayError):
    status = 503
    code = "no_healthy_broker"
    retryable = True


class FeedUnavailable(GatewayError):
    status = 503
    code = "feed_unavailable"
    retryable = True
