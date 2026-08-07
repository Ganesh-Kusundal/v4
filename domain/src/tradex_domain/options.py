"""Option chain objects (FDS 05 §6.3–§6.5, decisions D-2/D-3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from tradex_domain.errors import SDKError
from tradex_domain.instruments import Instrument, Option
from tradex_domain.serialization import Serializable
from tradex_domain.value_objects import Price


@dataclass(frozen=True, slots=True)
class OptionPair(Serializable):
    call: Option
    put: Option
    strike: Price


@dataclass(frozen=True, slots=True)
class Expiry(Serializable):
    underlying: Instrument
    expiry_date: date
    pairs: tuple[OptionPair, ...] = ()
    reference_price: Price | None = None

    def atm(self, offset: int = 0) -> OptionPair:
        if self.reference_price is None:
            raise SDKError("Expiry.reference_price is required for atm()")
        if not self.pairs:
            raise SDKError("Expiry has no pairs")
        index = _nearest_index(self.pairs, self.reference_price) + offset
        if index < 0 or index >= len(self.pairs):
            raise IndexError(f"atm offset {offset} out of range")
        return self.pairs[index]

    def otm(self, strikes: int = 5) -> list[OptionPair]:
        """Pairs on the away-from-spot side, ordered nearest-spot first (D-3)."""
        if self.reference_price is None:
            raise SDKError("Expiry.reference_price is required for otm()")
        away = [p for p in self.pairs if p.strike.value > self.reference_price.value]
        away.sort(key=lambda p: p.strike.value)
        return away[:strikes]

    def itm(self, strikes: int = 5) -> list[OptionPair]:
        """Pairs on the toward-spot side, ordered nearest-spot first (D-3)."""
        if self.reference_price is None:
            raise SDKError("Expiry.reference_price is required for itm()")
        toward = [p for p in self.pairs if p.strike.value < self.reference_price.value]
        toward.sort(key=lambda p: p.strike.value, reverse=True)
        return toward[:strikes]


@dataclass(frozen=True, slots=True)
class OptionChain(Serializable):
    underlying: Instrument
    _expiries: tuple[Expiry, ...] = ()

    def expiries(self) -> list[Expiry]:
        return list(self._expiries)

    def expiry(self, date: date) -> Expiry | None:
        for expiry in self._expiries:
            if expiry.expiry_date == date:
                return expiry
        return None


def _nearest_index(pairs: tuple[OptionPair, ...], reference: Price) -> int:
    return min(range(len(pairs)), key=lambda i: abs(pairs[i].strike.value - reference.value))


__all__ = ["Expiry", "OptionChain", "OptionPair"]
