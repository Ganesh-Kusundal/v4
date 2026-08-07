# ADR-004: Reactive Message Infrastructure Over an Imperative EventBus

## Status

Accepted

## Decision

Replace v3's imperative `EventBus` with an RxPY-based reactive message bus. The
core is `ReactiveBus` in `trading/src/tradex_trading/reactive/bus.py:20`, backed by
an RxPY `Subject` (`bus.py:28`). Every message is an Observable emission:

- `publish` (`bus.py:37`) appends to an optional message log and calls
  `subject.on_next`.
- `of_type` (`bus.py:52`) returns a filtered, `share()`d Observable of only
  messages of a given type (via `isinstance`), enabling typed streams like
  `bus.of_type(Quote).pipe(filter(...), map(...))`.
- `stream` (`bus.py:65`) exposes the raw Observable of all messages.
- `replay` (`bus.py:83`) re-emits the logged messages as an Observable sequence.
- `subscribe` (`bus.py:69`) tracks disposables in a `CompositeDisposable`
  (`bus.py:30`) torn down by `dispose` (`bus.py:100`).

A hardened wrapper, `BoundedReactiveBus` in
`trading/src/tradex_trading/reactive/bounded_bus.py:15`, layers safety on top:

- Subscriber exceptions are caught and routed to a dead-letter queue (DLQ)
  (`bounded_bus.py:44`).
- The message log is a bounded `deque(maxlen=max_log)`, default 10,000
  (`bounded_bus.py:36`), and the DLQ is bounded at 1,000 (`bounded_bus.py:33`).
- Optional metrics counters increment on publish and on dead-lettering
  (`bounded_bus.py:43`, `bounded_bus.py:48`).

SDK services consume the reactive bus directly — e.g. `StreamService` subscribes
typed quote/fill/depth streams via `bus.of_type(Quote)` in
`trading/src/tradex_trading/sdk/services/stream.py:36`, and `TradeService` receives
a `ReactiveBus` for order-flow events (`trade.py:34`).

## Context

v3's imperative `EventBus` scattered publish/subscribe logic and made it hard to
compose, filter, transform, or reason about message flows. Trading systems need
high-volume, ordered message flows (quotes, fills, depth) that benefit from
reactive operators, plus the ability to replay history and cleanly dispose
subscriptions. Reliability concerns (fast producers, slow/failing subscribers)
require bounded buffers and error isolation.

## Consequences

### Positive

- Composable: typed Observables (`of_type`) support `filter`/`map`/`share` and
  lazy backpressure through standard RxPY operators.
- Type-directed: `of_type` narrows streams by message class, so subscribers opt
  into exactly the event types they need.
- Replayable: the logged sequence supports historical re-emission via `replay`,
  useful for recovery and tests.
- Bounded and safe: `BoundedReactiveBus` prevents unbounded memory growth and
  isolates subscriber failures into a DLQ instead of crashing the publisher.
- Clean lifecycle: `CompositeDisposable` + `dispose` gives deterministic teardown
  of every subscription.
- Observable instrumentation via metrics counters aids operations.

### Negative

- Introduces a hard RxPY dependency in the trading package; the API surface is
  tied to `rx.Observable`/`rx.subject.Subject` types, coupling callers to the
  library.
- `of_type` filters by `isinstance`, so only exact message classes (or subclasses)
  match; structural/duck-typed events need explicit mapping.
- `ReactiveBus.publish` swallows and logs exceptions (`bus.py:43`) while
  `BoundedReactiveBus` adds its own try/except (`bounded_bus.py:40`) — error
  handling is duplicated across the two layers.
- `replay` ignores its `start`/`end` parameters (`bus.py:90`), so time-range replay
  is currently a stub despite being part of the API.
- The bounded log/DLQ are simple `deque`s with a single `threading.Lock`
  (`bounded_bus.py:32`); there is no explicit subscriber backpressure, so a slow
  consumer can still fall behind and messages are dropped at the DLQ maxlen
  without notification.
