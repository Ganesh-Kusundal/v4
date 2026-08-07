"""Wire-level instrument mapping (§9).

``InstrumentRegistry`` maps canonical ``InstrumentId`` values to provider-native
keys with a deterministic, reversible string encoding. Collisions are rejected
with ``SDKError``.

Thread-safety / atomic reload
----------------------------
The registry keeps its tables in a single immutable ``_RegistryState`` snapshot.
Readers (``resolve``, ``provider_key``, ``meta``) read the current snapshot
pointer without any lock, so a full-master reload via :meth:`replace_all` is
atomic: concurrent tick-resolution threads observe either the old or the new
complete state, never a partially-reloaded one. Incremental registrations
(``register`` / ``add_alias`` / ``register_authoritative`` — chain endpoints,
fallback universes) mutate the live snapshot in place under an internal write
lock, preserving today's semantics.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from tradex_domain.errors import SDKError
from tradex_domain.value_objects import InstrumentId

_TAG_BY_ASSET_CLASS = {
    "EQUITY": "EQ",
    "INDEX": "IDX",
    "FUTURE": "FUT",
    "OPTION": "OPT",
    "CURRENCY": "CUR",
    "COMMODITY": "COM",
    "ETF": "ETF",
}


def normalize_symbol(value: str) -> str:
    """Upper-case, strip whitespace, and drop common provider suffixes."""
    symbol = value.strip().upper()
    for suffix in ("-EQ", "-BE", "-FUT"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
            break
    return symbol


def normalize_exchange(value: str) -> str:
    return value.strip().upper()


@runtime_checkable
class WireAdapter(Protocol):
    """Reversible instrument mapping surface."""

    def instrument_key(self, instrument_id: InstrumentId) -> str: ...
    def reverse_instrument_key(self, key: str) -> InstrumentId | None: ...
    def resolve(self, name: str) -> InstrumentId | None: ...
    def register(self, instrument_id: InstrumentId, meta: dict[str, object]) -> None: ...
    def register_bulk(self, rows: list[dict[str, object]]) -> None: ...
    def add_alias(self, name: str, instrument_id: InstrumentId) -> None: ...


@dataclass(slots=True)
class _RegistryState:
    """Immutable-by-convention snapshot of all registry tables.

    The dict objects are mutated in place by incremental registrations under
    the registry's write lock, and replaced wholesale by ``replace_all``.
    Readers only ever touch the current snapshot pointer.
    """

    by_key: dict[str, InstrumentId] = field(default_factory=dict)
    by_instrument: dict[InstrumentId, dict[str, object]] = field(default_factory=dict)
    aliases: dict[str, InstrumentId] = field(default_factory=dict)
    keys_by_instrument: dict[InstrumentId, set[str]] = field(default_factory=dict)
    #: First-registered key per instrument — the canonical provider key.
    #: Later keys become aliases (never silently re-point the primary).
    primary_by_instrument: dict[InstrumentId, str] = field(default_factory=dict)


class InstrumentRegistry:
    """Deterministic, reversible, collision-rejecting instrument registry."""

    def __init__(self) -> None:
        self._state = _RegistryState()
        self._lock = threading.RLock()

    @classmethod
    def instrument_key(cls, instrument_id: InstrumentId) -> str:
        return cls._key(instrument_id, cls._tag_from_id(instrument_id))

    @staticmethod
    def _tag_from_id(instrument_id: InstrumentId) -> str:
        if instrument_id.right == "FUT":
            return "FUT"
        if instrument_id.right in {"CE", "PE"}:
            return "OPT"
        return "EQ"

    @staticmethod
    def _key(instrument_id: InstrumentId, tag: str) -> str:
        iid = instrument_id
        suffix = ""
        if iid.expiry is not None:
            suffix += ":" + iid.expiry.strftime("%Y%m%d")
        if iid.strike is not None:
            suffix += ":" + str(int(iid.strike))
        if iid.right is not None:
            suffix += ":" + iid.right
        return f"{iid.exchange}_{tag}|{iid.underlying}{suffix}"

    def _default_key(self, instrument_id: InstrumentId, meta: dict[str, object]) -> str:
        tag = _TAG_BY_ASSET_CLASS.get(str(meta.get("asset_class", "")).upper())
        if tag is None:
            tag = self._tag_from_id(instrument_id)
        return self._key(instrument_id, tag)

    # -- reads (lock-free; snapshot pointer) ----------------------------------

    def reverse_instrument_key(self, key: str) -> InstrumentId | None:
        return self._state.by_key.get(key)

    def provider_key(self, instrument_id: InstrumentId) -> str | None:
        """Return the canonical (first-registered) provider key for *instrument_id*.

        The first key ever registered for an instrument is the primary key;
        later keys (e.g. a REST chain endpoint registering a bare security id
        after the master registered ``MCX:<id>``) become aliases instead of
        silently re-pointing the primary.
        """
        return self._state.primary_by_instrument.get(instrument_id)

    def meta(self, instrument_id: InstrumentId) -> dict[str, object]:
        return dict(self._state.by_instrument.get(instrument_id, {}))

    def resolve(self, name: str) -> InstrumentId | None:
        stripped = name.strip()
        state = self._state
        direct = state.by_key.get(stripped)
        if direct is not None:
            return direct
        return state.aliases.get(stripped.upper())

    # -- writes (lock-guarded, mutate the live snapshot in place) -------------

    def register(self, instrument_id: InstrumentId, meta: dict[str, object]) -> None:
        """Register an instrument incrementally (chain endpoints, aliases).

        First registration wins as the primary provider key; later keys become
        aliases. New metadata is merged into the first registration's metadata
        (never dropped, never silently clobbered).
        """
        with self._lock:
            key = str(meta.get("key") or self._default_key(instrument_id, meta))
            self._put(key, instrument_id, meta)

    def register_authoritative(
        self,
        instrument_id: InstrumentId,
        key: str,
        meta: dict[str, object] | None = None,
    ) -> None:
        """Register *key* as the authoritative primary for *instrument_id*.

        Used by full-master loads (initial connect and daily refresh): the
        freshly downloaded master's key becomes the primary and any stale
        previously-registered keys for this instrument are dropped from
        reverse resolution, so a rotated/re-listed security id re-points on
        reload. Bare ids from REST chain endpoints stay aliases via
        ``register`` (never authoritative).
        """
        with self._lock:
            self._replace(key, instrument_id, dict(meta) if meta is not None else {"key": key})

    def register_bulk(self, rows: list[dict[str, object]]) -> None:
        """Register many rows atomically: validate all, then apply.

        A bulk registration is an *authoritative replacement* (a fresh master
        reload): each instrument's primary key is set to this batch's key and
        previously registered stale keys are dropped from reverse resolution.
        This is intentionally distinct from single ``register`` calls, which
        are incremental (first key wins, later keys become aliases).
        """
        with self._lock:
            prepared: list[tuple[str, InstrumentId, dict[str, object]]] = []
            for row in rows:
                symbol = normalize_symbol(str(row["symbol"]))
                exchange = normalize_exchange(str(row["exchange"]))
                iid = InstrumentId.equity(exchange, symbol)
                key = str(row.get("key") or self._default_key(iid, row))
                prepared.append((key, iid, dict(row)))
            pending: dict[str, InstrumentId] = dict(self._state.by_key)
            for key, iid, _meta in prepared:
                existing = pending.get(key)
                if existing is not None and existing != iid:
                    raise SDKError(f"instrument key collision: {key!r}")
                pending[key] = iid
            for key, iid, meta in prepared:
                self._replace(key, iid, meta)

    def add_alias(self, name: str, instrument_id: InstrumentId) -> None:
        with self._lock:
            self._state.aliases[name.strip().upper()] = instrument_id

    def replace_all(self, other: InstrumentRegistry) -> None:
        """Atomically replace this registry's state with *other*'s.

        Used by full-master reloads: the adapter builds a fresh registry off
        the parsed rows and swaps it in under the write lock, so concurrent
        readers see either the old or the new complete state — never a
        partially-reloaded one.

        Entries that *other* does not define (incremental chain-endpoint
        registrations or aliases for instruments absent from the fresh
        master) are carried over, matching the pre-reload semantics where a
        master reload never deletes unrelated registrations. Instruments the
        fresh master *does* define get exactly the master's keys/aliases —
        stale keys for them are dropped, so a rotated security id re-points.

        ``other`` must not be mutated concurrently with the swap.
        """
        with self._lock:
            src = other._state  # noqa: SLF001 — snapshot ownership transfers
            new_by_key: dict[str, InstrumentId] = dict(src.by_key)
            new_by_instrument: dict[InstrumentId, dict[str, object]] = {
                iid: dict(meta) for iid, meta in src.by_instrument.items()
            }
            new_aliases: dict[str, InstrumentId] = dict(src.aliases)
            new_keys_by_instrument: dict[InstrumentId, set[str]] = {
                iid: set(keys) for iid, keys in src.keys_by_instrument.items()
            }
            new_primary: dict[InstrumentId, str] = dict(src.primary_by_instrument)
            current = self._state
            for iid in current.by_instrument:
                if iid in new_by_instrument:
                    continue
                new_by_instrument[iid] = dict(current.by_instrument[iid])
                if iid in current.primary_by_instrument:
                    new_primary[iid] = current.primary_by_instrument[iid]
                if iid in current.keys_by_instrument:
                    keys = current.keys_by_instrument[iid]
                    new_keys_by_instrument[iid] = set(keys)
                    for key in keys:
                        new_by_key.setdefault(key, iid)
            # Carry over aliases for instruments the fresh master does not
            # define (including instruments carried over above) — a reload must
            # never drop the searchable name of a registration it kept.
            for name, iid in current.aliases.items():
                if iid not in src.by_instrument and name not in new_aliases:
                    new_aliases[name] = iid
            self._state = _RegistryState(
                by_key=new_by_key,
                by_instrument=new_by_instrument,
                aliases=new_aliases,
                keys_by_instrument=new_keys_by_instrument,
                primary_by_instrument=new_primary,
            )

    # -- internals (callers must hold ``_lock``) ------------------------------

    def _put(self, key: str, instrument_id: InstrumentId, meta: dict[str, object]) -> None:
        state = self._state
        existing = state.by_key.get(key)
        if existing is not None and existing != instrument_id:
            raise SDKError(
                f"instrument key collision: {key!r} maps to both {existing} and {instrument_id}"
            )
        # First registration wins as the primary provider key; every key for
        # the same instrument is kept as a resolvable alias. Registering a
        # second key must never delete the first (the MCX chain path registers
        # bare security ids after the master registered ``MCX:<id>``) nor
        # clobber the first registration's metadata (asset_class/raw that
        # history mapping depends on).
        if instrument_id not in state.primary_by_instrument:
            state.primary_by_instrument[instrument_id] = key
        state.by_key[key] = instrument_id
        state.keys_by_instrument.setdefault(instrument_id, set()).add(key)
        # Merge metadata: later registrations enrich the first (greeks,
        # lot_size, chain-derived fields) without dropping earlier fields.
        # The ``key`` field is never re-set by an alias registration — the
        # primary key is tracked separately in ``_primary_by_instrument`` and
        # must keep pointing at the first (or master-owned) key.
        existing_meta = state.by_instrument.get(instrument_id)
        if existing_meta is None:
            state.by_instrument[instrument_id] = dict(meta)
        else:
            merged = dict(existing_meta)
            for name, value in meta.items():
                if name == "key":
                    continue
                if value is not None and value != "":
                    merged[name] = value
            state.by_instrument[instrument_id] = merged

    def _replace(
        self, key: str, instrument_id: InstrumentId, meta: dict[str, object]
    ) -> None:
        """Authoritative replacement used by ``register_authoritative`` /
        ``register_bulk`` (master reload).

        The given key becomes the instrument's primary provider key and all
        previously registered keys for the same instrument are dropped from
        reverse resolution, so a fresh master never leaves stale keys
        resolving.
        """
        state = self._state
        existing = state.by_key.get(key)
        if existing is not None and existing != instrument_id:
            raise SDKError(
                f"instrument key collision: {key!r} maps to both {existing} and {instrument_id}"
            )
        old_keys = state.keys_by_instrument.get(instrument_id)
        if old_keys:
            for old_key in old_keys:
                if old_key != key and state.by_key.get(old_key) == instrument_id:
                    del state.by_key[old_key]
        state.primary_by_instrument[instrument_id] = key
        state.by_key[key] = instrument_id
        state.keys_by_instrument[instrument_id] = {key}
        state.by_instrument[instrument_id] = dict(meta)


__all__ = [
    "InstrumentRegistry",
    "WireAdapter",
    "normalize_exchange",
    "normalize_symbol",
]
