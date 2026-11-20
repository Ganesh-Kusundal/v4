# Money Handling Patterns and Best Practices

## 1. Overview

Financial calculations require exact decimal arithmetic. Using floating-point numbers (`float`) introduces rounding errors that are unacceptable in financial systems where precision is critical. Even small rounding errors can accumulate and lead to significant discrepancies in monetary calculations, regulatory reporting, and audit trails.

This document establishes the canonical patterns for handling money throughout TradeX v4, from domain value objects through wire contracts to cash ledger operations.

**Core principle**: `Decimal` everywhere for monetary values. No exceptions.

---

## 2. Domain Layer

### Value Objects

All monetary quantities are represented as frozen dataclasses with strict validation in `domain/src/tradex_domain/value_objects.py`.

#### Price

```python
@dataclass(frozen=True, slots=True)
class Price:
    value: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            raise TypeError("Price.value must be Decimal")
        if not self.value.is_finite():
            raise ValueError("Price must be finite (NaN/Infinity rejected)")
        if self.value < 0:
            raise ValueError("Price must be non-negative")
```

**Key properties**:
- Frozen dataclass prevents mutation after construction
- Rejects `NaN`, `Infinity`, and negative values
- Enforces `Decimal` type at construction time

#### Quantity

```python
@dataclass(frozen=True, slots=True)
class Quantity:
    value: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            raise TypeError("Quantity.value must be Decimal")
        if not self.value.is_finite():
            raise ValueError("Quantity must be finite (NaN/Infinity rejected)")
        # Note: negative quantities are valid for short positions.
```

**Key properties**:
- Allows negative values (short positions)
- Rejects `NaN` and `Infinity`
- Supports arithmetic operators (`+`, `-`, `*`, `__neg__`, `__abs__`)

#### Money

```python
@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: str = "INR"

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError("Money.amount must be Decimal")
        if not self.amount.is_finite():
            raise ValueError("Money.amount must be finite (NaN/Infinity rejected)")
```

**Key properties**:
- Allows negative amounts (losses, debits, negative P&L)
- Currency-aware (defaults to `"INR"`)
- Prevents cross-currency arithmetic via type checks on `__add__` / `__sub__`

### Why Frozen Dataclasses?

Frozen dataclasses provide immutability guarantees essential for financial correctness:

1. **No accidental mutation**: Once constructed, a `Price(Decimal("100"))` cannot change
2. **Thread-safe by default**: Immutable objects can be shared across threads without locks
3. **Hashable**: Can be used as dictionary keys or set members
4. **Clear ownership**: Values flow through the system; they are never modified in place

---

## 3. Wire Contracts

API boundaries enforce strict validation using Pydantic models with annotated types. All monetary fields use custom validators that convert incoming data to `Decimal` and reject invalid input with HTTP 422 responses.

### The `_to_decimal` Validator Pattern

Located in `interfaces/src/tradex_interfaces/contracts.py`:

```python
def _to_decimal(value: Any) -> Any:
    """Coerce a wire money value to ``Decimal``, or raise ``ValueError``.

    ``Decimal(str(value))`` raises ``decimal.InvalidOperation`` for anything
    that is not decimal-shaped (a mapping, a list, ``True``, ``"abc"``), and
    ``InvalidOperation`` is an ``ArithmeticError``, *not* a ``ValueError`` —
    so the route's ``except ValueError`` never saw it and the request
    answered 500 instead of 422. Raising ``ValueError`` here converts the
    failure at the boundary, where Pydantic turns it into a 422
    ``validation_error`` envelope before any handler runs.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"expected a decimal number, got {value!r}") from exc


#: Required money field (quantity, price, ...). ``None`` is rejected by the
#: ``Decimal`` type even though the validator lets it through.
_Money = Annotated[Decimal, BeforeValidator(_to_decimal)]

#: Optional money field (``price``, ``trigger_price``, protective legs).
_MoneyOpt = Annotated[Decimal | None, BeforeValidator(_to_decimal)]
```

**Critical design decision**: The validator catches `InvalidOperation` (an `ArithmeticError`) and re-raises as `ValueError`. This ensures Pydantic returns HTTP 422 (Bad Request) instead of HTTP 500 (Internal Server Error), providing appropriate error semantics at API boundaries.

### Usage in Request DTOs

```python
class PlaceOrderRequest(_Strict):
    instrument_id: str | None = None
    exchange: str | None = None
    symbol: str | None = None
    side: str
    order_type: str = "MARKET"
    quantity: _Money          # Required - cannot be None
    price: _MoneyOpt = None   # Optional - MARKET orders may omit
    trigger_price: _MoneyOpt = None
    time_in_force: str | None = None
    product: str | None = None
```

### Boundary Validation Behavior

| Input | Result |
|-------|--------|
| `"100"` | `Decimal("100")` |
| `100` | `Decimal("100")` |
| `100.50` | `Decimal("100.5")` |
| `None` (required field) | Rejected by Pydantic |
| `None` (optional field) | `None` |
| `"abc"` | HTTP 422: `"expected a decimal number, got 'abc'"` |
| `[]` | HTTP 422: `"expected a decimal number, got []"` |
| `True` | HTTP 422: `"expected a decimal number, got True"` |
| `{"key": "value"}` | HTTP 422: `"expected a decimal number, got {...}"` |

---

## 4. Cash Ledger Patterns

The cash ledger (`execution/src/tradex_execution/cash_ledger.py`) implements deterministic accounting for backtest and live execution. It distinguishes between SELL and BUY operations with fundamentally different cash requirements.

### SELL Self-Funding (MP-3)

SELL orders generate proceeds that fund their own fees. The cash debit formula is:

```python
# Net-proceeds proof (MP-3): a BUY is gated on notional + fee; a SELL
# only needs cash when the fee exceeds the proceeds (sells self-fund).
if side is OrderSide.BUY:
    debit = notional + amount
else:
    debit = max(Decimal(0), amount - notional)
```

**Example**: Selling 1 share at ₹100 with ₹1 fee:
- Notional: `1 × 100 = ₹100`
- Fee: `₹1`
- Debit: `max(0, 1 - 100) = 0` (no cash required)
- Net effect: Cash increases by `₹99`

This allows SELL orders to execute even with zero idle cash, since the sale proceeds cover the fee.

### BUY Cash-Gating

BUY orders require sufficient idle cash to cover both the notional and the fee:

```python
debit = notional + amount  # e.g., 100 + 1 = 101
```

**Example**: Buying 1 share at ₹100 with ₹1 fee:
- Notional: `1 × 100 = ₹100`
- Fee: `₹1`
- Debit: `100 + 1 = ₹101`
- Requires: At least ₹101 in idle cash

If insufficient cash is available, the ledger raises `ValueError` with a descriptive message:

```
insufficient buying power: fill <order_id> of 101 would take cash from 50 to -51
```

### Fee Calculation and Reservation

Fees are always debited from cash, regardless of order side:

```python
def apply_fill(
    self,
    side: OrderSide,
    quantity: Quantity,
    price: Price,
    fee: Decimal | Money,
    what: str,
    book: Callable[[], None] | None = None,
    skip_proof: bool = False,
    allow_overdraft: bool = False,
) -> None:
```

The atomic sequence within `apply_fill`:

1. **Affordability proof**: Check `debit` against current balance (no mutation)
2. **Position booking**: Execute `book()` callback under the same lock
3. **Notional**: BUY debits `price × qty`; SELL credits it
4. **Fee**: Always debited from cash

All four steps execute under a single `RLock`, ensuring no other thread can observe or race an intermediate state.

### Concurrency Guarantees

- One `RLock` per ledger instance guards all read-modify-write operations
- `apply_fill` is the **only** entry point for fill-related cash mutations
- `transaction()` context manager allows engines to extend the critical section across durable appends
- Lock order: ledger lock → position lock (never reversed, preventing deadlocks)

---

## 5. Common Pitfalls to Avoid

### Never Use `float` for Money

**Wrong**:
```python
price = 100.50  # float - introduces binary representation errors
total = price * quantity  # may produce 100.49999999999999 instead of 100.50
```

**Correct**:
```python
from decimal import Decimal

price = Decimal("100.50")  # exact decimal representation
total = price * quantity   # produces exactly 100.50
```

**Why**: IEEE 754 floating-point cannot represent many decimal fractions exactly. For example:
- `0.1 + 0.2 == 0.30000000000000004` (not `0.3`)
- Over thousands of trades, these errors accumulate

### Always Validate at Boundaries

Never trust external input. Use the `_to_decimal` validator pattern for all API inputs:

**Wrong**:
```python
# Directly passing unvalidated input to domain objects
price = Price(Decimal(request_data["price"]))  # may raise InvalidOperation → 500
```

**Correct**:
```python
# Use annotated Pydantic fields with _to_decimal validator
class MyRequest(BaseModel):
    price: _Money  # validated at boundary, returns 422 on invalid input
```

### Don't Catch `ArithmeticError` Silently

`Decimal` operations raise `InvalidOperation` (subclass of `ArithmeticError`) for invalid conversions. Catching this silently masks bugs:

**Wrong**:
```python
try:
    value = Decimal(user_input)
except ArithmeticError:
    value = Decimal(0)  # silent fallback hides bad data
```

**Correct**:
```python
try:
    value = Decimal(str(user_input))
except (InvalidOperation, ValueError) as exc:
    raise ValueError(f"expected a decimal number, got {user_input!r}") from exc
```

### Negative Quantities Are Valid

Do not assume quantities must be positive. Short positions use negative quantities:

```python
# Valid: short position
qty = Quantity(Decimal("-5"))

# Valid: long position
qty = Quantity(Decimal("5"))
```

Order-level validation enforces positive quantities for new orders, but portfolio tracking requires negative quantities for shorts.

### Money Can Be Negative

Negative `Money` amounts represent losses, debits, or negative P&L:

```python
# Valid: loss on a trade
pnl = Money(Decimal("-50"), "INR")

# Valid: fee debit
fee = Money(Decimal("-1.50"), "INR")
```

---

## 6. Testing Strategy

### Core Test Examples

#### SELL with Zero Idle Cash (`trading/tests/execution/test_sell_zero_idle_cash.py`)

```python
def test_cash_ledger_sell_zero_idle_cash_books() -> None:
    """SELL orders self-fund fees from proceeds."""
    ledger = CashLedger(initial=0)
    ledger.apply_fill(
        OrderSide.SELL,
        Quantity(value=Decimal("1")),
        Price(value=Decimal("100")),
        Decimal("1"),  # fee
        "fill x",
    )
    assert ledger.cash == Decimal("99")  # 100 - 1 = 99


def test_cash_ledger_buy_zero_cash_raises() -> None:
    """BUY orders require sufficient cash."""
    ledger = CashLedger(initial=0)
    with pytest.raises(ValueError):
        ledger.apply_fill(
            OrderSide.BUY,
            Quantity(value=Decimal("1")),
            Price(value=Decimal("100")),
            Decimal("1"),  # fee
            "fill x",
        )
```

#### Risk Manager Cash Gate (`trading/tests/execution/test_risk_cash_gate_fails_closed.py`)

```python
def test_raising_cash_provider_denies_when_fail_closed() -> None:
    """A provider that throws means the balance is unknown, not sufficient."""

    def _boom() -> Decimal:
        raise RuntimeError("broker funds unavailable")

    rm = RiskManager()
    rm.fail_closed_cash = True
    rm.bind_cash_provider(_boom)
    assert rm.check(_buy_request()) is False
    assert rm._last_deny_reason == "cash_unavailable"
```

### Edge Cases to Test

| Scenario | Expected Behavior |
|----------|------------------|
| Zero initial cash + SELL | Succeeds (self-funded) |
| Zero initial cash + BUY | Raises `ValueError` |
| NaN price/quantity/money | Raises `ValueError` at construction |
| Infinity price/quantity/money | Raises `ValueError` at construction |
| Negative price | Raises `ValueError` at construction |
| Negative quantity | Allowed (short positions) |
| Negative money amount | Allowed (losses, debits) |
| Cross-currency addition | Raises `ValueError` |
| Invalid string input (`"abc"`) | HTTP 422 at boundary |
| Boolean input (`True`) | HTTP 422 at boundary |
| List/dict input | HTTP 422 at boundary |
| Unbound cash provider + fail_closed | Denies order |
| Raising cash provider + fail_closed | Denies order |

### Validation Tests (`domain/tests/test_value_validation.py`)

```python
class TestPriceValidation:
    def test_negative_price_raises(self) -> None:
        with pytest.raises(ValueError, match="Price must be non-negative"):
            Price(Decimal("-100"))

    def test_nan_price_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            Price(Decimal("NaN"))

    def test_infinity_price_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            Price(Decimal("Infinity"))


class TestQuantityValidation:
    def test_negative_quantity_succeeds(self) -> None:
        """Negative quantities are valid for short positions."""
        q = Quantity(Decimal("-5"))
        assert q.value == Decimal("-5")


class TestMoneyValidation:
    def test_negative_money_succeeds(self) -> None:
        m = Money(Decimal("-50"))
        assert m.amount == Decimal("-50")

    def test_nan_money_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            Money(Decimal("NaN"))
```

---

## References

- [ADR 0003: Decimal-Everywhere Money Handling Pattern](../adr/0003-decimal-money-handling.md)
- Domain value objects: `domain/src/tradex_domain/value_objects.py`
- Wire contracts: `interfaces/src/tradex_interfaces/contracts.py`
- Cash ledger: `execution/src/tradex_execution/cash_ledger.py`
- MP-3: Self-funding fee mechanism for SELL orders
- Python `decimal` module documentation
- IEEE 754 floating-point standard limitations
