"""Canonical v4 value objects.

Per D-10: ``to_dict()`` / ``from_dict()`` on all domain objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from tradex_domain.enums import AssetClass, ExchangeId
from tradex_domain.serialization import from_dict, to_dict


def normalize_symbol(value: str, *, strip_provider_suffixes: bool = True) -> str:
    """Upper-case and strip whitespace; optionally drop provider suffixes.

    Single source of symbol normalization (merged the former divergent
    ``_normalize_symbol`` here [REF-3]).  ``InstrumentId`` normalizes with
    ``strip_provider_suffixes=False`` (raw underlying preserved); the wire
    registry's public helper strips ``-EQ/-BE/-FUT`` as before.
    """
    symbol = value.strip().upper()
    if strip_provider_suffixes:
        for suffix in ("-EQ", "-BE", "-FUT"):
            if symbol.endswith(suffix):
                symbol = symbol[: -len(suffix)]
                break
    return symbol


@dataclass(frozen=True, slots=True)
class InstrumentId:
    exchange: str
    underlying: str
    expiry: date | None = None
    strike: Decimal | None = None
    right: str | None = None
    asset_class: AssetClass = AssetClass.EQUITY

    VALID_EXCHANGES = {item.value for item in ExchangeId}
    VALID_RIGHTS = {"CE", "PE", "FUT"}

    def __post_init__(self) -> None:
        exchange = self.exchange.strip().upper()
        # Use known_exchanges() to include runtime-registered exchanges.
        from tradex_domain.enums import known_exchanges
        valid = set(known_exchanges())
        if exchange not in valid:
            raise ValueError(f"Invalid exchange: {self.exchange!r}")
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(
            self,
            "underlying",
            normalize_symbol(self.underlying, strip_provider_suffixes=False),
        )
        if self.strike is not None and not isinstance(self.strike, Decimal):
            object.__setattr__(self, "strike", Decimal(str(self.strike)))
        if self.right is not None:
            right = self.right.strip().upper()
            if right not in self.VALID_RIGHTS:
                raise ValueError(f"Invalid right: {self.right!r}")
            object.__setattr__(self, "right", right)

    @classmethod
    def equity(cls, exchange: str, symbol: str) -> InstrumentId:
        return cls(exchange=exchange, underlying=symbol, asset_class=AssetClass.EQUITY)

    @classmethod
    def future(cls, exchange: str, underlying: str, expiry: date) -> InstrumentId:
        return cls(
            exchange=exchange, underlying=underlying, expiry=expiry,
            right="FUT", asset_class=AssetClass.FUTURE,
        )

    @classmethod
    def option(
        cls,
        exchange: str,
        underlying: str,
        expiry: date,
        strike: Decimal | float,
        right: str,
    ) -> InstrumentId:
        return cls(
            exchange=exchange, underlying=underlying, expiry=expiry,
            strike=strike if isinstance(strike, Decimal) else Decimal(str(strike)),
            right=right, asset_class=AssetClass.OPTION,
        )

    @classmethod
    def index(cls, exchange: str, symbol: str) -> InstrumentId:
        """Create an instrument ID for an index."""
        return cls(exchange=exchange, underlying=symbol, asset_class=AssetClass.INDEX)

    @classmethod
    def currency(cls, exchange: str, symbol: str) -> InstrumentId:
        """Create an instrument ID for a currency pair."""
        return cls(exchange=exchange, underlying=symbol, asset_class=AssetClass.CURRENCY)

    @classmethod
    def commodity(cls, exchange: str, symbol: str) -> InstrumentId:
        """Create an instrument ID for a commodity."""
        return cls(exchange=exchange, underlying=symbol, asset_class=AssetClass.COMMODITY)

    @classmethod
    def parse(cls, value: str) -> InstrumentId:
        parts = value.strip().split(":")
        if len(parts) < 2:
            raise ValueError(f"Invalid InstrumentId format: {value!r}")
        exchange = parts[0].upper()
        underlying = parts[1].upper()
        expiry: date | None = None
        strike: Decimal | None = None
        right: str | None = None
        if len(parts) >= 3 and parts[2]:
            if parts[2].upper() == "FUT":
                right = "FUT"
            elif len(parts[2]) == 8:
                try:
                    expiry = datetime.strptime(parts[2], "%Y%m%d").date()
                except ValueError as exc:
                    raise ValueError(f"Invalid InstrumentId format: {value!r}") from exc
            else:
                raise ValueError(f"Invalid InstrumentId format: {value!r}")
        if len(parts) >= 4 and parts[3]:
            if parts[3].upper() in cls.VALID_RIGHTS:
                right = parts[3].upper()
            else:
                try:
                    strike = Decimal(parts[3])
                except (TypeError, ValueError, InvalidOperation) as exc:
                    raise ValueError(f"Invalid InstrumentId format: {value!r}") from exc
        if len(parts) >= 5 and parts[4]:
            right = parts[4].upper()
        if expiry and right is None:
            right = "FUT"
        if right == "FUT":
            asset_class = AssetClass.FUTURE
        elif right in {"CE", "PE"}:
            asset_class = AssetClass.OPTION
        else:
            asset_class = AssetClass.EQUITY
        return cls(
            exchange=exchange, underlying=underlying, expiry=expiry, strike=strike,
            right=right, asset_class=asset_class,
        )

    def __str__(self) -> str:
        parts = [self.exchange, self.underlying]
        if self.expiry is not None:
            parts.append(self.expiry.strftime("%Y%m%d"))
        if self.strike is not None:
            parts.append(
                str(int(self.strike))
                if self.strike == self.strike.to_integral_value()
                else str(self.strike)
            )
        if self.right is not None:
            parts.append(self.right)
        return ":".join(parts)

    def __repr__(self) -> str:
        return (
            f"InstrumentId(exchange={self.exchange!r}, underlying={self.underlying!r}, "
            f"expiry={self.expiry!r}, strike={self.strike!r}, right={self.right!r}, "
            f"asset_class={self.asset_class!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, InstrumentId):
            return NotImplemented
        # Identity is the key (exchange + underlying + derivative fields).
        # ``asset_class`` is an advisory hint used only for provider-key tag
        # generation (wire._tag_from_id); it is intentionally excluded from
        # equality so a registry registered via ``InstrumentId.equity`` still
        # resolves an ``InstrumentId.index`` with the same exchange/underlying.
        return (
            self.exchange,
            self.underlying,
            self.expiry,
            self.strike,
            self.right,
        ) == (
            other.exchange,
            other.underlying,
            other.expiry,
            other.strike,
            other.right,
        )

    def __hash__(self) -> int:
        return hash((self.exchange, self.underlying, self.expiry, self.strike, self.right))

    def to_dict(self) -> dict[str, object]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> InstrumentId:
        return from_dict(cls, data)


@dataclass(frozen=True, slots=True)
class OrderId:
    value: str

    def __str__(self) -> str:
        return str(self.value)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.value!r})"

    def to_dict(self) -> dict[str, object]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> OrderId:
        return from_dict(cls, data)


@dataclass(frozen=True, slots=True)
class AccountId:
    value: str

    def to_dict(self) -> dict[str, object]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AccountId:
        return from_dict(cls, data)


@dataclass(frozen=True, slots=True)
class CorrelationId:
    value: UUID | str

    def __str__(self) -> str:
        return str(self.value)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.value!r})"

    def to_dict(self) -> dict[str, object]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> CorrelationId:
        return from_dict(cls, data)


@dataclass(frozen=True, slots=True)
class Price:
    value: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            raise TypeError("Price.value must be Decimal")
        if not self.value.is_finite():
            raise ValueError("Price must be finite (NaN/Infinity rejected)")
        if self.value < 0:
            raise ValueError("Price must be non-negative")

    def __str__(self) -> str:
        return str(self.value)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.value!r})"

    # -- comparison operators --

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Price):
            return NotImplemented
        return self.value < other.value

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Price):
            return NotImplemented
        return self.value <= other.value

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Price):
            return NotImplemented
        return self.value > other.value

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Price):
            return NotImplemented
        return self.value >= other.value

    # -- arithmetic operators --

    def __sub__(self, other: object) -> Price:
        if not isinstance(other, Price):
            return NotImplemented
        return Price(self.value - other.value)

    def to_dict(self) -> dict[str, object]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Price:
        return from_dict(cls, data)


@dataclass(frozen=True, slots=True)
class Quantity:
    value: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            raise TypeError("Quantity.value must be Decimal")
        if not self.value.is_finite():
            raise ValueError("Quantity must be finite (NaN/Infinity rejected)")
        # Note: negative quantities are valid for short positions.
        # Order-level validation (OrderRequest) enforces positive quantity for orders.

    def __str__(self) -> str:
        return str(self.value)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.value!r})"

    # -- arithmetic operators --

    def __add__(self, other: object) -> Quantity:
        if not isinstance(other, Quantity):
            return NotImplemented
        return Quantity(self.value + other.value)

    def __sub__(self, other: object) -> Quantity:
        if not isinstance(other, Quantity):
            return NotImplemented
        return Quantity(self.value - other.value)

    def __mul__(self, other: object) -> Quantity | Money:
        if isinstance(other, Price):
            return Money(amount=self.value * other.value, currency="INR")
        if isinstance(other, (Decimal, int, float)):
            return Quantity(self.value * Decimal(str(other)))
        return NotImplemented

    def __neg__(self) -> Quantity:
        return Quantity(-self.value)

    def __abs__(self) -> Quantity:
        return Quantity(abs(self.value))

    def __bool__(self) -> bool:
        return self.value != 0

    # -- comparison operators --

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Quantity):
            return NotImplemented
        return self.value < other.value

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Quantity):
            return NotImplemented
        return self.value <= other.value

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Quantity):
            return NotImplemented
        return self.value > other.value

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Quantity):
            return NotImplemented
        return self.value >= other.value

    def to_dict(self) -> dict[str, object]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Quantity:
        return from_dict(cls, data)


@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: str = "INR"

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError("Money.amount must be Decimal")
        if not self.amount.is_finite():
            raise ValueError("Money.amount must be finite (NaN/Infinity rejected)")

    # -- arithmetic operators --

    def __add__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        if self.currency != other.currency:
            raise ValueError(
                f"Cannot add Money with different currencies: {self.currency} != {other.currency}"
            )
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def __sub__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        if self.currency != other.currency:
            raise ValueError(
                f"Cannot subtract Money with different currencies: "
                f"{self.currency} != {other.currency}"
            )
        return Money(amount=self.amount - other.amount, currency=self.currency)

    def __neg__(self) -> Money:
        return Money(amount=-self.amount, currency=self.currency)

    def __abs__(self) -> Money:
        return Money(amount=abs(self.amount), currency=self.currency)

    def __bool__(self) -> bool:
        return self.amount != 0

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"

    def __repr__(self) -> str:
        return f"Money({self.amount!r}, {self.currency!r})"

    def to_dict(self) -> dict[str, object]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Money:
        return from_dict(cls, data)


__all__ = [
    "AccountId",
    "CorrelationId",
    "InstrumentId",
    "Money",
    "OrderId",
    "Price",
    "Quantity",
]
