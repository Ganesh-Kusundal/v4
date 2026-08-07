# ADR-003: Instrument Registry as a Reversible, Collision-Rejecting Map

## Status

Accepted

## Decision

Map canonical domain `InstrumentId` values to provider-native keys through the
`InstrumentRegistry` class in `domain/src/tradex_domain/wire.py:83`, exposed to
brokers via the `WireAdapter` protocol (`wire.py:54`). The encoding is
deterministic and reversible:

- A canonical key is built by `instrument_key`/`_key` (`wire.py:91`, `wire.py:103`)
  as `f"{exchange}_{tag}|{underlying}{:expiry}{:strike}{:right}"`, where the tag
  derives from the instrument's asset class or right/strike fields
  (`_tag_from_id`, `wire.py:95`, and `_TAG_BY_ASSET_CLASS`, `wire.py:28`).
- `reverse_instrument_key` (`wire.py:122`) resolves a provider key back to an
  `InstrumentId`; `resolve` (`wire.py:138`) resolves a symbol against primary keys
  and then upper-cased aliases.
- Provider keys and symbols are normalized before matching via `normalize_symbol`
  (`wire.py:39`, strips `-EQ`/`-BE`/`-FUT` suffixes) and `normalize_exchange`
  (`wire.py:49`).

Two registration regimes coexist (`wire.py:148` onward):

- **Incremental** (`register`, `add_alias`, `register_authoritative`-style chain
  endpoints): first key registered wins as the primary provider key
  (`primary_by_instrument`); later keys become aliases and never silently re-point
  or clobber the first registration's metadata.
- **Authoritative replacement** (`register_bulk`, `register_authoritative`,
  `replace_all`): a fresh master reload makes the batch's key primary and drops
  stale keys for that instrument, so a rotated/re-listed security id re-points on
  reload.

All state lives in an immutable-by-convention `_RegistryState` snapshot
(`wire.py:66`) swapped atomically under an internal `threading.RLock`. Readers
(`resolve`, `provider_key`, `meta`) read the snapshot pointer lock-free, so a
full-master reload is atomic: concurrent tick-resolution threads see either the old
or the new complete state. Key collisions raise `SDKError` (`wire.py:198`,
`wire.py:268`).

Broker-side loading is shared in
`brokers/src/tradex_brokers/common/instruments.py`, which parses master files
(`load_master_csv`, `load_master_json`) and extracts futures chains
(`future_chain_from_master`), surfacing parse/load failures as
`InstrumentNotFoundError`.

## Context

Brokers identify instruments with divergent native keys (Dhan/Upstox master files,
chain-endpoint security ids, `MCX:<id>` forms), while strategies and analytics want
a single canonical `InstrumentId`. Symbol spelling, exchange casing, and suffixes
differ across providers, so mapping must be normalized, unambiguous, and safe to
reload (daily master refresh) without corrupting concurrent reads.

## Consequences

### Positive

- Deterministic, reversible encoding lets one canonical `InstrumentId` round-trip
  to a stable provider key and back.
- Atomic snapshot reloads give lock-free reads and no torn state during a daily
  master refresh.
- Collision rejection (`SDKError`) prevents two different instruments silently
  sharing a key.
- First-wins-primary semantics mean a bare security id from a chain endpoint never
  clobbers the master's `MCX:<id>` primary; metadata merges instead of dropping.
- A reload drops only stale keys for instruments the fresh master defines,
  preserving unrelated incremental registrations and aliases.

### Negative

- The registry holds several cross-indexed tables (`by_key`, `by_instrument`,
  `aliases`, `keys_by_instrument`, `primary_by_instrument`), and keeping them
  consistent is non-trivial — `_put`, `_replace`, and `replace_all` carry subtle
  invariants.
- Two registration regimes (incremental vs. authoritative) are easy to misuse: a
  caller invoking `register` expecting a reload gets first-wins aliasing instead.
- The canonical key format (`EXCHANGE_TAG|SYMBOL:...`) is a string convention with
  no schema enforcement; future tag/format changes would break reversibility.
- A single `threading.RLock` serializes all writes; extremely high-frequency
  incremental registration could become a contention point.
