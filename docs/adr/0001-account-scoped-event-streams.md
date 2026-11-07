# Account-Scoped Event Streams

## Status

accepted

## Date

2026-07-31

## Context

Before Wave 1, events could be filed under a generic "system" stream without explicit account association. This design created a cross-account contamination risk where events from different tenants could intermingle in the same stream partition. The lack of mandatory stream declarations meant that new event types inherited implicit routing behavior, making it difficult to enforce multi-tenant isolation at the event store layer.

Key problems:
- Events lacked required stream classification, enabling accidental cross-account leakage
- The event store had no mechanism to validate which event classes were permitted in a given stream
- Stream partitions were not tied to tenant boundaries (broker:account:stream), undermining data isolation guarantees
- Security audits could not verify tenant separation at the event persistence layer

Constraints:
- Must maintain backward compatibility during migration for existing event types
- Must support multiple broker backends (Kafka, RabbitMQ, etc.)
- Must not introduce runtime performance overhead beyond initial class loading

## Decision

All `DomainEvent` subclasses must declare a `ClassVar[str] stream` attribute that explicitly names the event stream to which instances of that event type belong. The event store derives the set of allowed classnames for each stream from these declarations and enforces membership at write time. Streams are partitioned using the pattern `broker:account:stream`, ensuring physical or logical separation per tenant.

Implementation details:
- Every concrete `DomainEvent` subclass defines `stream: ClassVar[str] = "<stream-name>"`
- The event store maintains a registry mapping stream names to allowed event class qualified names
- At write time, the store validates that the event's declared stream matches the target partition
- Partition keys incorporate the account identifier: `{broker}:{account_id}:{stream_name}`
- During initialization, the system scans all loaded `DomainEvent` subclasses and builds the stream-to-classname index

This approach makes stream assignment explicit, auditable, and enforced rather than implicit or derived.

## Consequences

### Positive Consequences

- Prevents cross-account data leakage by enforcing stream-level tenant isolation
- Enables multi-tenant architectures where each account's events are physically or logically separated
- Provides clear audit trail: every event type declares its intended stream at the class level
- Event store can reject misrouted events at write time, catching configuration errors early
- Simplifies compliance verification for data residency and tenant separation requirements

### Negative Consequences

- Requires explicit `stream` declaration for every new event type, adding boilerplate
- Developers must understand stream naming conventions and partition semantics when creating events
- Migration of existing event types requires adding stream declarations and potentially reprocessing historical data
- Incorrect stream declarations will cause runtime write failures, requiring careful testing

### Trade-offs

Slightly more boilerplate vs safety: Each event class gains one additional required field (`stream: ClassVar[str]`), but this eliminates an entire class of security vulnerabilities related to cross-account contamination. The explicit declaration also serves as documentation, making the intended routing visible at the point of definition.

## Alternatives Considered

### Alternative 1: Keep System-Wide Stream

Maintain the original design where events default to a shared "system" stream unless otherwise specified. Rejected because it preserves the cross-account contamination risk and provides no enforcement mechanism for tenant isolation. Multi-tenant deployments would require application-layer filtering rather than infrastructure-level guarantees.

### Alternative 2: Auto-Derive Streams from Event Names

Automatically assign streams based on event class name patterns (e.g., `AccountCreated` -> `accounts` stream). Rejected because implicit derivation introduces "magic" behavior that is hard to reason about, debug, and audit. Name-based heuristics can produce incorrect assignments when naming conventions evolve or when domain boundaries shift. Explicit declaration ensures intent is clear and stable.

## References

- [ADR Template](template.md)
- Domain Event Store implementation: `src/domain/events/store.py`
- Base DomainEvent class: `src/domain/events/base.py`
