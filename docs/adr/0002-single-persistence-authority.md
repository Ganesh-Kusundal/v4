# ADR 0002: Single Persistence Authority

## Status
Accepted

## Context
Before Wave 1, the system maintained multiple order stores:
- SQLite event store (primary)
- Duplicate order stores (secondary)

This architecture created significant consistency risks, including:
- Divergent state between stores
- Complex synchronization logic
- Difficult recovery procedures
- Multiple points of failure for order state

## Decision
The SQLite event store is designated as the **ONLY** source of truth for order state. All duplicate order stores have been deleted as part of Task 14.

Additionally, the `_attach_event_store` fail-open mechanism has been deleted as part of this decision, ensuring that all operations must succeed against the single authoritative store.

## Consequences

### Positive
- Eliminates consistency bugs caused by divergent stores
- Simplifies recovery procedures
- Enables single hash chain verification for audit integrity
- Reduces operational complexity

### Negative
- All order queries must go through the event store
- May require a caching layer in the future for performance optimization

### Trade-off
Simplicity and consistency are prioritized over potential query performance. The decision accepts that some queries may be slower without a dedicated read-optimized store, but gains significant reliability and maintainability benefits.

## Alternatives Considered

### Keep dual stores with synchronization
Rejected due to eventual consistency problems and the complexity of maintaining reliable sync mechanisms between stores.

### Use event sourcing for some entities only
Rejected because partial event sourcing would introduce inconsistent architectural patterns and only partially address the complexity problem.
