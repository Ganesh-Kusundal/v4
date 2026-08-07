"""Instrument hierarchy (FDS 05 §3/§6, decisions D-1/D-4/D-11).

``Instrument`` is an abstract base. Concrete types are value objects; they
never own services (D-1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from tradex_domain.enums import AssetClass, ExchangeId, InstrumentType
from tradex_domain.serialization import Serializable
from tradex_domain.value_objects import InstrumentId, Price


@dataclass(frozen=True, slots=True)
class InstrumentMeta(Serializable):
    description: str | None = None
    isin: str | None = None
    lot_size: int | None = None
    tick_size: Price | None = None
    extra: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Instrument(Serializable):
    """Abstract base instrument. Passive value object — carries no services (D-1)."""

    instrument_id: InstrumentId
    symbol: str
    exchange: ExchangeId
    asset_class: AssetClass
    currency: str = "INR"
    instrument_type: InstrumentType = InstrumentType.EQUITY
    expiry: date | None = None
    strike: Decimal | None = None
    option_type: str | None = None
    contract_size: Decimal | None = None
    lot_size: Decimal | None = None
    tick_size: Decimal | None = None
    meta: InstrumentMeta = field(default_factory=InstrumentMeta)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())

    @property
    def underlying(self) -> str:
        return self.instrument_id.underlying


@dataclass(frozen=True, slots=True)
class Equity(Instrument):
    asset_class: AssetClass = AssetClass.EQUITY
    instrument_type: InstrumentType = InstrumentType.EQUITY

    @classmethod
    def of(cls, exchange: str, symbol: str) -> Equity:
        return cls(
            instrument_id=InstrumentId.equity(exchange, symbol),
            symbol=symbol,
            exchange=ExchangeId(exchange.strip().upper()),
        )


@dataclass(frozen=True, slots=True)
class Index(Instrument):
    asset_class: AssetClass = AssetClass.INDEX
    instrument_type: InstrumentType = InstrumentType.INDEX

    @classmethod
    def of(cls, exchange: str, symbol: str) -> Index:
        return cls(
            instrument_id=InstrumentId.equity(exchange, symbol),
            symbol=symbol,
            exchange=ExchangeId(exchange.strip().upper()),
        )


@dataclass(frozen=True, slots=True)
class Future(Instrument):
    asset_class: AssetClass = AssetClass.FUTURE
    instrument_type: InstrumentType = InstrumentType.FUTURE

    @classmethod
    def of(cls, exchange: str, underlying: str, expiry: date) -> Future:
        return cls(
            instrument_id=InstrumentId.future(exchange, underlying, expiry),
            symbol=underlying,
            exchange=ExchangeId(exchange.strip().upper()),
            expiry=expiry,
        )


@dataclass(frozen=True, slots=True)
class Option(Instrument):
    right: str = "CE"
    asset_class: AssetClass = AssetClass.OPTION
    instrument_type: InstrumentType = InstrumentType.OPTION

    def __post_init__(self) -> None:
        Instrument.__post_init__(self)
        right = self.right.strip().upper()
        if right not in {"CE", "PE"}:
            raise ValueError(f"Invalid option right: {self.right!r}")
        object.__setattr__(self, "right", right)
        object.__setattr__(self, "option_type", right)

    @classmethod
    def of(
        cls,
        exchange: str,
        underlying: str,
        expiry: date,
        strike: Decimal | float,
        right: str,
    ) -> Option:
        return cls(
            instrument_id=InstrumentId.option(exchange, underlying, expiry, strike, right),
            symbol=underlying,
            exchange=ExchangeId(exchange.strip().upper()),
            expiry=expiry,
            strike=strike if isinstance(strike, Decimal) else Decimal(str(strike)),
            right=right,
        )


@dataclass(frozen=True, slots=True)
class Currency(Instrument):
    asset_class: AssetClass = AssetClass.CURRENCY
    instrument_type: InstrumentType = InstrumentType.CURRENCY

    @classmethod
    def of(cls, exchange: str, symbol: str) -> Currency:
        return cls(
            instrument_id=InstrumentId.equity(exchange, symbol),
            symbol=symbol,
            exchange=ExchangeId(exchange.strip().upper()),
        )


@dataclass(frozen=True, slots=True)
class Commodity(Instrument):
    asset_class: AssetClass = AssetClass.COMMODITY
    instrument_type: InstrumentType = InstrumentType.COMMODITY

    @classmethod
    def of(cls, exchange: str, symbol: str) -> Commodity:
        return cls(
            instrument_id=InstrumentId.equity(exchange, symbol),
            symbol=symbol,
            exchange=ExchangeId(exchange.strip().upper()),
        )


__all__ = [
    "Commodity",
    "Currency",
    "Equity",
    "Future",
    "Index",
    "Instrument",
    "InstrumentMeta",
    "Option",
]
