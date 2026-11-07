# ADR 0003: Decimal-Everywhere Money Handling Pattern

## Status

Accepted

## Context

Financial calculations require exact decimal arithmetic. Using floating-point numbers (`float`) introduces rounding errors that are unacceptable in financial systems where precision is critical. Even small rounding errors can accumulate and lead to significant discrepancies in monetary calculations.

## Decision

All monetary values throughout the codebase use Python's `Decimal` type exclusively:

### Domain Value Objects
- `Price.value`: `Decimal`
- `Quantity.value`: `Decimal`
- `Money.amount`: `Decimal`

### Wire Contracts
- `_Money = Annotated[Decimal, BeforeValidator(_to_decimal)]`
- All API inputs and outputs use this annotated type to ensure consistent validation

### Order Execution (MP-3)
- SELL orders self-fund fees from proceeds: `debit = max(Decimal(0), amount - notional)`
- Cash ledger distinguishes SELL (cash-independent) from BUY (cash-gated) operations

## Implementation Details

### Validator Behavior
The `_to_decimal` validator raises `ValueError` (not `InvalidOperation`) when conversion fails. This ensures Pydantic returns HTTP 422 (Bad Request) instead of HTTP 500 (Internal Server Error), providing appropriate error semantics at API boundaries.

### Boundary Validation
- All monetary values are validated at wire contract boundaries using the `_to_decimal` validator
- No `float` usage is permitted in domain money fields (verified by grep)
- Decimal precision is maintained throughout all financial calculations

## Consequences

### Positive
- **Exact arithmetic**: Eliminates floating-point rounding errors entirely
- **No surprises**: Predictable behavior in all financial calculations
- **Proper financial precision**: Supports arbitrary precision as needed for regulatory compliance

### Negative
- **Performance**: Decimal operations are slower than float operations (acceptable trade-off for correctness)
- **Validation overhead**: Requires explicit validation at all system boundaries

### Trade-off
This decision prioritizes correctness over performance. The performance impact is acceptable given the critical nature of financial accuracy requirements.

## References

- MP-3: Self-funding fee mechanism for SELL orders
- Python `decimal` module documentation
- IEEE 754 floating-point standard limitations
