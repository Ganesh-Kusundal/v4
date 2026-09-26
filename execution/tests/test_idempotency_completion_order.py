"""A correlation id must be completed only once the money has actually moved.

Defect (Critical, M-3): ``_run_pipeline`` completed the idempotency key at
``record_result(reserved_cid, order.order_id)`` *before* the fill block. That
block books the position, debits the notional, debits the fee and advances the
OMS, and it can raise part-way: the cash ledger's buying-power floor rejects a
debit that would overdraw the account. When it did, the position was already
booked, the fee was not applied, the OMS still read ``FILLED`` with
``filled_quantity=0``, and the correlation id was already COMPLETED — so the
client's retry replayed the same receipt and the divergence became permanent.
Rebuilding from the event log then produced a *different* book than the live
one, because the log held an ``OrderFilled`` the live book had only partly
applied.

The invariant pinned here: when the fill block does not complete, nothing is
half-applied and the correlation id is released so the request can be retried.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, OrderType
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, Price, Quantity
from tradex_execution.engine import ExecutionEngine
from tradex_execution.idempotency import MemoryIdempotencyGuard
from tradex_reactive.bus import ReactiveBus

_INSTRUMENT = Equity.of("NSE", "RELIANCE")
_CID = CorrelationId("m3-torn-fill")


def _request(cid: CorrelationId, qty: str = "10", price: str = "2500") -> OrderRequest:
    return OrderRequest(
        instrument=_INSTRUMENT,
        side=OrderSide.BUY,
        quantity=Quantity(Decimal(qty)),
        order_type=OrderType.MARKET,
        price=Price(Decimal(price)),
        correlation_id=cid,
    )


def _engine(cash: str) -> ExecutionEngine:
    return ExecutionEngine(
        ReactiveBus(),
        _offline_fill_source(),
        idempotency_guard=MemoryIdempotencyGuard(),
        cash=Decimal(cash),
        allow_negative_cash=False,
    )


def _offline_fill_source():
    from tradex_execution.fill_sources import SimulatedFillSource

    return SimulatedFillSource()


def test_overdrawing_fill_leaves_no_partial_state_and_keeps_the_key_retryable() -> None:
    """A fill cash cannot cover must not book a position or burn the cid."""
    # 1,000 of cash cannot cover 10 x 2,500 = 25,000 of notional.
    engine = _engine("1000")

    with pytest.raises(ValueError, match="buying power"):
        engine.submit(_request(_CID))

    # Nothing half-applied: no position was booked.
    assert engine.cache.get_position(_INSTRUMENT) is None, (
        "position was booked even though the cash debit failed"
    )
    # Cash untouched (the floor rejects before mutating).
    assert engine.cash_snapshot().cash == Decimal("1000")

    # And the correlation id must NOT be completed, or the client can never
    # retry: a replay would hand back the same receipt while the divergence
    # persists.
    guard = engine._guard  # noqa: SLF001 - the invariant is about the guard
    assert _CID.value not in getattr(guard, "_completed", {}), (
        "correlation id was completed before the money moved; a retry would "
        "replay the receipt and make the divergence permanent"
    )


def test_affordable_fill_completes_the_key_normally() -> None:
    """The success path must still complete the key (replay stays idempotent)."""
    engine = _engine("100000")
    receipt = engine.submit(_request(CorrelationId("m3-ok")))
    assert receipt.status.value == "FILLED"

    guard = engine._guard  # noqa: SLF001
    assert "m3-ok" in getattr(guard, "_completed", {}), (
        "a completed fill must record its result for replay"
    )
    assert engine.cache.get_position(_INSTRUMENT) is not None
    assert engine.cash_snapshot().cash == Decimal("75000")


def test_retry_after_a_failed_fill_is_not_replayed_as_success() -> None:
    """A retry of an unfunded request must not be answered from the log."""
    from tradex_domain.execution import OrderReceipt

    engine = _engine("1000")
    with pytest.raises(ValueError, match="buying power"):
        engine.submit(_request(_CID))

    # Fund the account and retry: the request must actually execute, not
    # replay a receipt for an order that never took effect. A replay comes back
    # as a bare OrderId (the idempotency-replay return shape) rather than an
    # OrderReceipt, so the type is the tell.
    engine.restore_cash(Decimal("100000"))
    result = engine.submit(_request(_CID))
    assert isinstance(result, OrderReceipt), (
        "retry after a failed fill was replayed from the log instead of "
        f"executed (returned {type(result).__name__})"
    )
    assert result.status.value == "FILLED"
    position = engine.cache.get_position(_INSTRUMENT)
    assert position is not None, "retry booked no position"
    assert position.quantity.value == Decimal("10"), (
        "expected exactly one fill of 10; the failed attempt must not have "
        "booked a position that the retry then doubled"
    )
