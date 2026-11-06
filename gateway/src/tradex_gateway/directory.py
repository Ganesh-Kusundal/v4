"""Canonical instrument resolution shared by every gateway service."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from tradex_brokers.common.instruments import instrument_from_id
from tradex_domain.enums import AssetClass
from tradex_domain.instruments import Instrument, Option
from tradex_domain.value_objects import InstrumentId

from tradex_gateway.brokers import BrokerRuntime
from tradex_gateway.errors import BadRequest, InstrumentNotFound


def parse_canonical(text: str, asset_class: str | None = None) -> InstrumentId:
    """Parse an external ID; ``asset_class=index`` normalizes to ``IDX``."""
    raw = text.strip().upper()
    if not raw:
        raise BadRequest("instrument id is required")
    if asset_class and asset_class.strip().lower() == "index":
        parts = [part for part in raw.split(":") if part]
        if not parts:
            raise BadRequest(f"invalid instrument id: {text!r}")
        return InstrumentId.index("IDX", parts[-1])
    if raw.startswith("IDX:"):
        parts = [part for part in raw.split(":") if part]
        if len(parts) < 2:
            raise BadRequest(f"invalid index id: {text!r}")
        return InstrumentId.index("IDX", parts[1])
    try:
        return InstrumentId.parse(raw)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc


@dataclass(frozen=True)
class ResolvedInstrument:
    instrument_id: InstrumentId
    instrument: Instrument
    brokers: tuple[str, ...]
    per_broker: Mapping[str, Instrument | None]
    lot_sizes: Mapping[str, int | None] = field(default_factory=dict)


@dataclass
class _ExpiryEntry:
    value: date | None
    expires_at: float


class InstrumentDirectory:
    def __init__(
        self,
        runtimes: Mapping[str, BrokerRuntime],
        *,
        clock: Callable[[], float] = time.monotonic,
        expiry_ttl: float = 300.0,
    ) -> None:
        self._runtimes = runtimes
        self._clock = clock
        self._expiry_ttl = expiry_ttl
        self._expiry_cache: dict[str, _ExpiryEntry] = {}
        self._lock = threading.Lock()

    def resolve(self, text: str, asset_class: str | None = None) -> ResolvedInstrument:
        instrument_id = parse_canonical(text, asset_class)
        per_broker: dict[str, Instrument | None] = {}
        lot_sizes: dict[str, int | None] = {}
        brokers: list[str] = []
        for name, runtime in self._runtimes.items():
            instrument = runtime.instrument(instrument_id)
            per_broker[name] = instrument
            lot_sizes[name] = runtime.lot_size(instrument_id)
            if instrument is not None and runtime.available and runtime.known(instrument_id):
                brokers.append(name)
        try:
            canonical = instrument_from_id(instrument_id)
        except Exception as exc:  # noqa: BLE001 - malformed derivative IDs
            raise InstrumentNotFound(str(exc)) from exc
        if not brokers and not any(runtime.available for runtime in self._runtimes.values()):
            raise InstrumentNotFound("no broker is available")
        return ResolvedInstrument(
            instrument_id=instrument_id,
            instrument=canonical,
            brokers=tuple(brokers),
            per_broker=per_broker,
            lot_sizes=lot_sizes,
        )

    def instrument(self, text: str, asset_class: str | None = None) -> Instrument:
        return self.resolve(text, asset_class).instrument

    def lot_size(self, instrument_id: InstrumentId | str, broker: str | None = None) -> int | None:
        if isinstance(instrument_id, InstrumentId):
            iid = instrument_id
        else:
            try:
                iid = parse_canonical(str(instrument_id))
            except BadRequest:
                return None
        names: list[str] = []
        if broker and broker in self._runtimes:
            names.append(broker)
        names.extend(name for name in self._runtimes if name not in names)
        for name in names:
            value = self._runtimes[name].lot_size(iid)
            if value is not None:
                return value
        return None

    def candidates(
        self,
        *,
        asset_class: str | None = None,
        limit: int = 1,
    ) -> list[Instrument]:
        """Instruments of *asset_class* known to any runtime (bounded by *limit*)."""
        wanted = asset_class.strip().lower() if asset_class else None
        found: list[Instrument] = []
        seen: set[str] = set()
        for runtime in self._runtimes.values():
            if not runtime.available:
                continue
            universe = getattr(runtime.broker, "_universe", None)
            items: Iterable[Instrument]
            if callable(universe):
                try:
                    items = universe()
                except Exception:  # noqa: BLE001 - universe is best effort
                    continue
            else:
                continue
            for instrument in items:
                key = str(instrument.instrument_id)
                if key in seen:
                    continue
                if wanted and str(instrument.asset_class.value).lower() != wanted:
                    continue
                seen.add(key)
                found.append(instrument)
                if len(found) >= limit:
                    return found
        return found

    def search(
        self,
        query: str,
        *,
        asset_class: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        wanted = asset_class.strip().lower() if asset_class else None
        merged: dict[str, dict[str, Any]] = {}
        for name, runtime in self._runtimes.items():
            for instrument in runtime.search(query):
                if wanted and str(instrument.asset_class.value).lower() != wanted:
                    continue
                key = str(instrument.instrument_id)
                entry = merged.setdefault(
                    key,
                    {"instrument": instrument, "brokers": set(), "lot_sizes": {}},
                )
                entry["brokers"].add(name)
                lot_size = runtime.lot_size(instrument.instrument_id)
                if lot_size is not None:
                    entry["lot_sizes"][name] = lot_size
        rows = [
            self.instrument_meta(entry["instrument"], sorted(entry["brokers"]), entry["lot_sizes"])
            for entry in merged.values()
        ]
        rows.sort(key=lambda row: (row["asset_class"], row["instrument_id"]))
        return rows[:limit]

    def nearest_expiry(self, underlying: Instrument) -> date | None:
        key = str(underlying.instrument_id)
        now = self._clock()
        with self._lock:
            cached = self._expiry_cache.get(key)
            if cached is not None and cached.expires_at > now:
                return cached.value
        today = date.today()
        best: date | None = None
        for runtime in self._runtimes.values():
            if not runtime.available:
                continue
            for instrument in runtime.search(underlying.symbol):
                if not isinstance(instrument, Option):
                    continue
                if instrument.expiry is None or instrument.expiry < today:
                    continue
                if instrument.instrument_id.underlying.upper() != underlying.symbol.upper():
                    continue
                if best is None or instrument.expiry < best:
                    best = instrument.expiry
        with self._lock:
            self._expiry_cache[key] = _ExpiryEntry(best, now + self._expiry_ttl)
        return best

    @staticmethod
    def instrument_meta(
        instrument: Instrument,
        brokers: Iterable[str] = (),
        lot_sizes: Mapping[str, int | None] | None = None,
    ) -> dict[str, Any]:
        broker_list = list(brokers)
        sizes = dict(lot_sizes or {})
        resolved_lot_size = next(
            (sizes[name] for name in broker_list if sizes.get(name) is not None),
            None,
        )
        return {
            "instrument_id": str(instrument.instrument_id),
            "symbol": instrument.symbol,
            "exchange": str(instrument.exchange),
            "asset_class": str(instrument.asset_class.value).lower(),
            "expiry": instrument.expiry.isoformat() if instrument.expiry else None,
            "strike": str(instrument.strike) if instrument.strike is not None else None,
            "right": instrument.option_type,
            "lot_size": resolved_lot_size,
            "lot_size_by_broker": {
                name: value for name, value in sizes.items() if value is not None
            },
            "brokers": sorted(broker_list),
        }

    @staticmethod
    def asset_class_of(asset_class: str | None) -> AssetClass | None:
        if asset_class is None:
            return None
        try:
            return AssetClass(asset_class.strip().upper())
        except ValueError:
            return None
