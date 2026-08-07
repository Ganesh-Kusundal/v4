# ADR-002: Type Safety Through a Fail-Closed Capability Matrix

## Status

Accepted

## Decision

Represent broker capabilities as a single frozen `dataclass` truth table,
`BrokerCapabilities`, in `domain/src/tradex_domain/capabilities.py:17`, with all
flags defaulting to `False`. Capability presence is gated at runtime by the single
loud check `require_capability` (`capabilities.py:152`), which raises
`CapabilityNotSupportedError` when the named flag is false. Each broker supplies
its own table via a factory: `dhan_capabilities` (`capabilities.py:50`),
`upstox_capabilities` (`capabilities.py:86`), and `paper_capabilities`
(`capabilities.py:120`).

SDK services consume the table rather than branching on broker identity. For
example, `TradeService` maps order types to capability flags in
`_TYPE_CAPABILITY` (`trading/src/tradex_trading/sdk/services/trade.py:24`) and
calls `require_capability` before submitting (`trade.py:52`); `StreamService`
requires `supports_portfolio_stream` before binding a position stream
(`trading/src/tradex_trading/sdk/services/stream.py:76`).

## Context

Brokers differ materially in what they support — Dhan supports `supports_edis`
and `supports_super_order`, Upstox supports `supports_news`/`supports_fundamentals`
and a 30-level depth feed, while the paper backend supports almost nothing.
Relying on capability flags at call sites keeps behavior explicit and fail-closed,
so a broker can never silently claim a feature it does not provide.

## Consequences

### Positive

- **Fail-closed by default**: the dataclass defaults every flag to `False`, so an
  unconfigured `BrokerCapabilities()` admits no capabilities until a broker's truth
  table explicitly enables them.
- **Single loud gate**: `require_capability` centralizes the error path, raising
  `CapabilityNotSupportedError` with a descriptive message instead of failing
  obscurely inside a broker call.
- **No broker-identity branching**: services key off capability flags, not
  `BrokerId`, so new brokers need no edits to service logic.
- Machine-relevant limits travel with the capabilities: `max_batch_size`,
  `depth_levels`, `max_stream_instruments`, and `supported_asset_classes` capture
  real per-connection constraints (Dhan 1000 instruments/20 depth levels, Upstox
  500/30).
- The `frozen=True, slots=True` dataclass is immutable and compact, safe to share
  across threads and cheap to allocate.

### Negative

- Capabilities are a closed enum of boolean flags; new features require editing
  the dataclass and every broker's truth table.
- The matrix is a contract of intent, not enforcement — nothing forces a broker's
  implementation to actually match its declared table, so a wrong truth table
  produces misleading errors.
- Two flags encode the same intent (`STOP` and `STOP_LIMIT` both map to
  `supports_stop_order` in `_TYPE_CAPABILITY`), which can under-report broker
  support.
- Only a subset of flags is checked at call sites today; unchecked flags remain
  advisory documentation.
