# ADR-001: Protocol Boundaries Between the Domain and Broker Adapters

## Status

Accepted

## Context

TradeX v4 must support multiple Indian brokers (Dhan, Upstox) plus a paper-trading
backend through a single, uniform interface. v3 coupled strategies and the SDK
directly to broker-specific implementations, so swapping brokers or adding a new
one required changing calling code. We need a stable contract that lives at the
domain layer, does not force concrete dependencies on either side, and lets each
broker expose the broker-specific features it actually supports.

The domain package is the only module both brokers and the SDK may import; it must
not depend on broker or trading internals.

## Decision

Define broker adapter boundaries as runtime-checkable `typing.Protocol` interfaces
in `domain/src/tradex_domain/protocols.py`, rather than abstract base classes or
broker-specific interfaces:

- `BrokerAdapter` (`protocols.py:29`) declares the full adapter surface a broker
  must satisfy: lifecycle (`connect`/`close`), orders (`submit_order`,
  `cancel_order`, `modify_order`, `get_order`, `get_orderbook`), portfolio
  (`get_positions`, `get_holdings`, `get_account`, `get_portfolio`), market data
  (`get_quote`, `ltp`, `depth`, `history`, `get_option_chain`, `search`), streaming
  (`stream_backend`, `market_stream_backend`, `depth_stream_backend`,
  `subscribe_quotes`, `subscribe_depth`, `unsubscribe`), and instruments
  (`load_instruments`).
- `ExtensionAdapter` (`protocols.py:111`) extends `BrokerAdapter` to expose
  capability-gated, broker-specific orders: `submit_super_order`,
  `submit_forever_order`, `submit_slice_order`, `submit_edis`.
- `SessionFacade` (`protocols.py:126`) declares the minimal surface a strategy
  reaches through on a trading session. Its service properties (`market`, `trade`,
  `portfolio`, `stream`, `scanner`, `analytics`, `extension`, `state`, `bus`) are
  typed as `object` precisely because the domain cannot import trading types — the
  protocol exists for IDE autocomplete hints, not strict type checking.

Both `BrokerAdapter` and `ExtensionAdapter` are `@runtime_checkable`, so the SDK can
use `isinstance`-style checks without importing broker packages.

## Consequences

### Positive

- A broker is a drop-in: any object structurally matching `BrokerAdapter` works,
  with no inheritance requirement and no shared broker base class.
- The domain stays dependency-free of broker/SDK internals; `SessionFacade` keeps
  `object` typing rather than importing trading types.
- Runtime checkability lets the SDK test capability presence and extension
  presence dynamically.
- Adding a broker (e.g. Zerodha) only requires implementing the protocol, not
  touching SDK or strategy code.

### Negative

- Structural protocols are enforced only by shape, not by construction — a
  partial implementation (missing a method) fails at runtime, not import time,
  unless a static type checker is run.
- `SessionFacade`'s `object`-typed properties provide no static guarantees, so
  autocomplete is a hint rather than a contract; misuse is caught by the consumer,
  not by the type system.
- Large protocols like `BrokerAdapter` create a wide surface every adapter must
  implement even when many methods go unused.
