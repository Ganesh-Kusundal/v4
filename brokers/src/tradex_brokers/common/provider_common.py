"""Shared utility functions for broker adapters.

Common helpers used across Dhan, Upstox, and Paper adapters for response
validation, value parsing, and auth verification.

Instrument loading, building, chain derivation, and provider-key resolution
now live in :mod:`tradex_brokers.common.instruments` (the single source of
truth for broker-side instrument utilities) and are re-exported here for
backwards compatibility.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from tradex_domain import (
    AuthenticationError,
    SDKError,
)
from tradex_domain.datetime_formats import DASHED_DATE, DASHED_DATETIME

from tradex_brokers.common.instruments import (
    as_decimal,
    as_price,
    build_instrument_from_row,
    future_chain_from_master,
    instrument_from_id,
    option_chain_from_master,
    parse_date,
    provider_key,
    resolve_instrument,
)

log = logging.getLogger(__name__)

# Business rejection codes/messages that mean the durable token is invalid
_TOKEN_REJECTION_MARKERS = (
    "dh-901",
    "dh-906",
    "invalid_authentication",
    "invalid token",
    "invalid_token",
    "unauthorized",
    "udapi100050",
    "authentication failed",
)


def _has_token_rejection_marker(text: str) -> bool:
    """Check if text contains any token rejection markers."""
    lowered = text.lower()
    return any(marker in lowered for marker in _TOKEN_REJECTION_MARKERS)


def is_token_rejection_response(status: int, body: object) -> bool:
    """Classify provider token rejection before domain normalization."""
    if status in {401, 403}:
        return True
    return status == 400 and _has_token_rejection_marker(str(body))


def is_token_rejection_error(exc: Exception) -> bool:
    """True when an adapter exception signals the broker rejected the token."""
    lowered = str(exc).lower()
    if any(phrase in lowered for phrase in ("http 401", "http 403", "http 400")):
        return True
    return _has_token_rejection_marker(lowered)


def require_success(response: dict) -> dict:
    """Validate that a broker API response indicates success.

    Checks for common error indicators in the response dict and raises
    ``SDKError`` subclasses when the response signals failure.

    Parameters
    ----------
    response:
        Parsed JSON response from the broker API.

    Returns
    -------
    dict
        The same *response* dict if it indicates success.

    Raises
    ------
    AuthenticationError
        If the response indicates an authentication failure (401/403).
    SDKError
        If the response contains an error status or message.
    """
    if not isinstance(response, dict):
        raise SDKError(f"Expected dict response, got {type(response).__name__}")

    # Check HTTP status code if available (embedded by transport layer)
    http_status = response.get("_http_status")
    if http_status is not None:
        if http_status in (401, 403):
            raise AuthenticationError(
                f"Authentication failed (HTTP {http_status})"
            )
        if http_status == 429:
            from tradex_domain import RateLimitError
            raise RateLimitError(f"Rate limit exceeded (HTTP {http_status})")
        if http_status >= 500:
            from tradex_domain import BrokerUnavailableError
            raise BrokerUnavailableError(f"Server error (HTTP {http_status})")
        if http_status >= 400:
            raise SDKError(f"Client error (HTTP {http_status})")

    # Check for HTTP-style status codes
    status = response.get("status")
    if status is not None:
        status_str = str(status).lower()
        if status_str in ("error", "failed", "failure"):
            error_msg = response.get("errors", response.get("message", "Unknown error"))
            raise SDKError(f"Broker API error: {error_msg}")

    # Check for explicit error field
    if response.get("error"):
        error_detail = response["error"]
        if isinstance(error_detail, dict):
            error_msg = error_detail.get("message", str(error_detail))
        else:
            error_msg = str(error_detail)
        raise SDKError(f"Broker API error: {error_msg}")

    # Check for common HTTP error code patterns
    code = response.get("code")
    if code is not None:
        code_int = int(code) if not isinstance(code, int) else code
        if code_int in (401, 403):
            raise AuthenticationError(
                f"Authentication failed (HTTP {code_int}): {response.get('message', '')}"
            )
        if code_int >= 400:
            raise SDKError(
                f"Broker API error (HTTP {code_int}): {response.get('message', '')}"
            )

    return response


def parse_timestamp(value: str | int | float) -> datetime:
    """Parse a timestamp from various formats into a timezone-aware datetime.

    Supports:
    - Unix epoch seconds (int/float)
    - Unix epoch milliseconds (int > 1e12)
    - ISO 8601 strings
    - Common broker date formats

    Parameters
    ----------
    value:
        The timestamp value.

    Returns
    -------
    datetime
        Timezone-aware datetime in UTC.

    Raises
    ------
    SDKError
        If the value cannot be parsed.
    """
    if isinstance(value, (int, float)):
        # Distinguish seconds from milliseconds
        if value > 1e12:
            value = value / 1000.0
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OSError, ValueError, OverflowError) as exc:
            raise SDKError(f"Cannot parse timestamp {value!r}") from exc

    if isinstance(value, str):
        value = value.strip()
        # Tolerant-parsing fallback ladder, tried in order. This is an ORDERED
        # LIST OF DISTINCT WIRE SHAPES, not duplicated literals: brokers each
        # emit their own spelling for the same instant, and an entry may only
        # be dropped once that provider is proven never to send it. Collapsing
        # entries (or reordering them) would reject valid-but-unusual
        # responses. The first four are the ISO-8601 'T' variants; the
        # space-separated and date-only entries reuse the domain format
        # contracts so the wire shapes have exactly one spelling.
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",  # ISO 8601, aware
            "%Y-%m-%dT%H:%M:%S.%f%z",  # ISO 8601, aware, microseconds
            "%Y-%m-%dT%H:%M:%S",  # ISO 8601, naive
            "%Y-%m-%dT%H:%M:%S.%f",  # ISO 8601, naive, microseconds
            DASHED_DATETIME,  # "%Y-%m-%d %H:%M:%S"  -- space-separated
            "%Y-%m-%d %H:%M:%S%z",  # space-separated, aware (no domain constant)
            DASHED_DATE,  # "%Y-%m-%d"  -- date only
            "%d-%m-%Y",  # day-first, European (no domain constant)
            "%d/%m/%Y",  # day-first, slashed (no domain constant)
        ):
            try:
                dt = datetime.strptime(value, fmt)  # noqa: DTZ007
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt
            except ValueError:
                continue
        raise SDKError(f"Cannot parse timestamp string {value!r}")

    raise SDKError(f"Cannot parse timestamp of type {type(value).__name__}")


def first_mapping(value: object) -> Mapping[str, Any]:
    """Extract first mapping from a value (dict or list of dicts)."""
    if isinstance(value, Mapping):
        return value
    if isinstance(value, list) and value and isinstance(value[0], Mapping):
        return value[0]
    return {}


def unwrap_data(payload: object) -> object:
    """Unwrap 'data' field from payload if present."""
    if isinstance(payload, Mapping) and "data" in payload:
        return payload["data"]
    return payload


__all__ = [
    "as_decimal",
    "as_price",
    "build_instrument_from_row",
    "first_mapping",
    "future_chain_from_master",
    "instrument_from_id",
    "is_token_rejection_error",
    "is_token_rejection_response",
    "option_chain_from_master",
    "parse_date",
    "parse_timestamp",
    "provider_key",
    "require_success",
    "resolve_instrument",
    "unwrap_data",
]
