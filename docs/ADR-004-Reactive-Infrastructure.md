# ADR-004: Reactive Message Infrastructure Over an Imperative EventBus

## Status

Accepted — amended 2026-08-07 (ordering + thread-safety contract, see
"Amendment 2026-08-07" below)

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
- `ReactiveBus.publish` swallows and logs exceptions while `BoundedReactiveBus`
  adds its own try/except — error handling is duplicated across the two layers.
- `replay` ignores its `start`/`end` parameters, so time-range replay is
  currently a stub despite being part of the API.
- The bounded log/DLQ are simple `deque`s; there is no explicit subscriber
  backpressure, so a slow consumer can still fall behind and messages are
  dropped at the DLQ maxlen without notification.

## Amendment 2026-08-07: Ordering & Thread-Safety Contract

`ReactiveBus.publish` (`trading/src/tradex_trading/reactive/bus.py`) is a
**synchronous, latency-neutral drain queue**: a nested `publish()` made from
inside a subscriber is enqueued into an in-flight deque and drained by the
outermost frame before `publish()` returns.

### Contract

- **Synchronous delivery, latency-neutral.** All effects of a `publish()`
  (including every nested chain) complete before it returns; there is no
  buffering across calls and no lock in the hot path. Live market events are
  never delayed by ordering — the drain reuses the work the old recursive
  delivery already did (same `Subject.on_next` calls, deque instead of stack
  recursion).
- **Causal stream order: stream == log.** Nested publishes are delivered after
  the triggering message's full delivery, so stream order matches the message
  log and is independent of subscription timing. This replaced the previous
  depth-first recursion, where a stream recorder attached after a publisher
  saw a chain's effects *before* the triggering message.
- **Single-threaded `ReactiveBus`.** The drain state (`_pending`/`_draining`)
  is not locked; publish from one thread, or wrap the bus in
  `ThreadSafeReactiveBus` for concurrent publishers.
- **`ThreadSafeReactiveBus` / `BoundedReactiveBus` for concurrent publishers.**
  Both serialize `publish()` with an `RLock` (previously a non-reentrant
  `Lock`, which deadlocked when a subscriber published through the wrapper
  during delivery — now re-entered safely and enqueued by the core drain).
- **Live wiring.** `boot(mode="live")` (`runtime/startup.py`) and
  `TradingSession.live()` (`sdk/session.py`) use `ThreadSafeReactiveBus`,
  because the broker feed thread, engine worker threads, and API callers
  publish concurrently and a plain RxPY `Subject` must never be driven from
  two threads at once. Paper/backtest/replay sessions keep the plain bus
  (single-threaded). The feed thread's own quote → pipeline chain is
  reentrant on the same thread, so the lock never delays it.
- **Nested-delivery cap.** A subscriber that republishes forever fails
  visibly: the drain stops after 10,000 nested deliveries per publish, logs
  an error, and drops the backlog — instead of the old `RecursionError`
  (recursion) or an unbounded silent loop (queue).

### Consequences

Positive: deterministic, subscription-order-independent streams; one truth
(log == stream); no stack recursion on cascades; the live race (concurrent
`Subject.on_next` from feed/engine/API threads) is closed at the composition
root without touching the hot core.

Negative: the plain bus is single-threaded-only — publishing it from multiple
threads (e.g. a paper session driven through `AsyncTradingSession`'s thread
pool) must go through `ThreadSafeReactiveBus`; and a deadlock regression in
the wrappers would hang (not fail) a reentrant publish until a timeout.
