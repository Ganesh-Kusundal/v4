"""Canonical v4 enums.

Consolidated per D-7 (single ``Timeframe`` StrEnum) and FDS 05 §7.3
(order lifecycle states NEW/ACK).
"""

from __future__ import annotations

from enum import StrEnum


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class OrderStatus(StrEnum):
    NEW = "NEW"
    PENDING = "PENDING"
    ACK = "ACK"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    SUBMITTED = "SUBMITTED"
    UNKNOWN = "UNKNOWN"


class TimeInForce(StrEnum):
    DAY = "DAY"


class ProductType(StrEnum):
    INTRADAY = "INTRADAY"
    DELIVERY = "DELIVERY"
    MARGIN = "MARGIN"
    MTF = "MTF"
    COVER_ORDER = "COVER_ORDER"

    @classmethod
    def from_wire(cls, value: str | ProductType | None) -> ProductType:
        """Parse wire token (MIS, CNC, NRML, MTF) into canonical ProductType."""
        if value is None:
            return cls.INTRADAY
        if isinstance(value, cls):
            return value
        token = str(value).strip().upper()
        mapping = {
            "MIS": cls.INTRADAY,
            "CNC": cls.DELIVERY,
            "NRML": cls.MARGIN,
            "MTF": cls.MTF,
            "INTRADAY": cls.INTRADAY,
            "DELIVERY": cls.DELIVERY,
            "MARGIN": cls.MARGIN,
        }
        if token in mapping:
            return mapping[token]
        raise ValueError(f"unknown product type: {value!r}")


class AssetClass(StrEnum):
    EQUITY = "EQUITY"
    INDEX = "INDEX"
    FUTURE = "FUTURE"
    OPTION = "OPTION"
    CURRENCY = "CURRENCY"
    COMMODITY = "COMMODITY"


class ExchangeId(StrEnum):
    NSE = "NSE"
    BSE = "BSE"
    NFO = "NFO"
    BFO = "BFO"
    MCX = "MCX"
    CDS = "CDS"
    BCD = "BCD"
    NSE_COMM = "NSE_COMM"
    IDX = "IDX"


class BrokerId(StrEnum):
    DHAN = "DHAN"
    UPSTOX = "UPSTOX"
    PAPER = "PAPER"
    REPLAY = "REPLAY"


class Timeframe(StrEnum):
    """Single canonical timeframe type."""

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    D1 = "1d"
    W1 = "1w"


# ---------------------------------------------------------------------------
# Open enum registration (extensibility)
# ---------------------------------------------------------------------------

_CUSTOM_BROKER_IDS: dict[str, str] = {}
_CUSTOM_EXCHANGE_IDS: dict[str, str] = {}


def register_broker(broker_id: str, label: str | None = None) -> str:
    """Register a custom broker ID at runtime.

    Parameters
    ----------
    broker_id : str
        The broker identifier.
    label : str | None
        Human-readable label (optional).

    Returns
    -------
    str
        The registered broker ID.
    """
    _CUSTOM_BROKER_IDS[broker_id.upper()] = label or broker_id
    return broker_id.upper()


def register_exchange(exchange_id: str, label: str | None = None) -> str:
    """Register a custom exchange ID at runtime.

    Parameters
    ----------
    exchange_id : str
        The exchange identifier.
    label : str | None
        Human-readable label (optional).

    Returns
    -------
    str
        The registered exchange ID.
    """
    _CUSTOM_EXCHANGE_IDS[exchange_id.upper()] = label or exchange_id
    return exchange_id.upper()


def known_brokers() -> dict[str, str]:
    """Return all known broker IDs (built-in + registered)."""
    built_in = {b.value: b.name for b in BrokerId}
    return {**built_in, **_CUSTOM_BROKER_IDS}


def known_exchanges() -> dict[str, str]:
    """Return all known exchange IDs (built-in + registered)."""
    built_in = {e.value: e.name for e in ExchangeId}
    return {**built_in, **_CUSTOM_EXCHANGE_IDS}


__all__ = [
    "AssetClass",
    "BrokerId",
    "ExchangeId",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "ProductType",
    "TimeInForce",
    "Timeframe",
    "register_broker",
    "register_exchange",
    "known_brokers",
    "known_exchanges",
]
