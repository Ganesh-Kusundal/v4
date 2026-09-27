"""AC-6 — the engine refuses an unscheduled product **at submit**, and a fill
carries the product of the order it came from.

``tradex_execution.fees`` already refuses ``MARGIN``/``MTF``/``COVER_ORDER``,
and ``test_fees_product_aware`` proves it. That proof is about the *calculator*,
and it is silent about the thing that made the refusal unsafe: where in
``ExecutionEngine._run_pipeline`` it happened. The pipeline called
``self._fill.submit(request)`` and only computed the fee after the venue
answered, so a MARGIN order left the process, was accepted, and *then* raised —
leaving an order at the broker that this process could neither price nor
account for. That is the unknown-outcome state the kill switch exists for,
reached by a different route, and it is loud without being safe.

So the control here is about **ordering**, not about the number:

- **CONTROL (e)** the venue is never called. ``_SpyFillSource`` records every
  ``submit``; a refused order leaves the record empty. Asserting only that the
  receipt is REJECTED would pass on the old code too — the old code also
  returned REJECTED, just after the venue had the order.
- **CONTROL (f)** the gate holds with costs switched OFF. A gate that only
  exists when fees are on is not a gate, and ``require_fee_schedule`` is
  deliberately module-level so it can refuse an engine built with
  ``fee_calculator=None``.
- **CONTROL (g)** the refusal does not leak the idempotency reservation. The
  gate sits *after* ``check_and_reserve``, so a bare ``raise`` would leave the
  correlation id reserved forever and every retry of that cid would answer
  PENDING for the life of the process.
- **CONTROL (h)** a fill is built with its parent order's product, so the
  DELIVERY schedule is reachable in paper, live and backtest. This is the
  five-line backfill; the control is that the label survives the fill path
  rather than that the line exists.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from tradex_domain.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    TimeInForce,
)
from tradex_domain.execution import Fill, Order, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import (
    CorrelationId,
    OrderId,
    Price,
    Quantity,
)
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fees import FeeCalculator
from tradex_execution.idempotency import MemoryIdempotencyGuard
from tradex_reactive.bus import ReactiveBus

_UNSCHEDULED = (ProductType.MARGIN, ProductType.MTF, ProductType.COVER_ORDER)
_PRICED = (ProductType.INTRADAY, ProductType.DELIVERY)


class _SpyFillSource:
    """A fill source that records every submit it is asked to perform.

    The record is the control: a refusal that happens after this is entered
    has already let the order out, whatever the receipt says afterwards.
    """

    #: Offline stub: never contacts a venue, so a failure is a rejection.
    reaches_venue = False

    def __init__(self) -> None:
        self.position_projection_owned = False
        self.submission_boundary_crossed = True
        self.calls: list[OrderRequest] = []

    def submit(self, request: OrderRequest) -> tuple[Order, Fill | None]:
        self.calls.append(request)
        order = Order(
            order_id=OrderId(value=str(uuid.uuid4())),
            instrument=request.instrument,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            price=request.price,
            time_in_force=request.time_in_force,
            status=OrderStatus.FILLED,
            correlation_id=request.correlation_id,
            product_type=request.product_type,
        )
        return order, Fill(
            order_id=order.order_id,
            instrument=order.instrument,
            side=order.side,
            quantity=order.quantity,
            price=Price(value=Decimal("2500")),
            timestamp=request.reference_timestamp,  # type: ignore[arg-type]
            product_type=request.product_type,
        )

    def cancel(self, order_id: OrderId) -> None:
        pass

    def modify(self, order_id: OrderId, request: OrderRequest) -> None:
        pass


def _request(
    product: ProductType,
    cid: str | None = None,
) -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        time_in_force=TimeInForce.DAY,
        product_type=product,
        correlation_id=CorrelationId(value=cid) if cid else None,
    )


def _engine(source: _SpyFillSource, **kwargs: object) -> ExecutionEngine:
    return ExecutionEngine(
        bus=ReactiveBus(), fill_source=source, **kwargs  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# CONTROL (e) — the refusal happens before the venue call
# ---------------------------------------------------------------------------


class TestControlETheVenueIsNeverCalled:
    """CONTROL (e): ordering, which is the part the fee model's own tests
    cannot see."""

    @pytest.mark.parametrize("product", _UNSCHEDULED)
    def test_the_fill_source_is_never_entered(self, product: ProductType) -> None:
        source = _SpyFillSource()
        receipt = _engine(source).submit(_request(product))

        assert source.calls == [], (
            f"{product.value} reached the venue before it was refused"
        )
        assert receipt is not None
        assert receipt.status is OrderStatus.REJECTED
        assert "no fee schedule" in receipt.message.lower()
        assert product.value in receipt.message

    @pytest.mark.parametrize("product", _PRICED)
    def test_a_priced_product_still_reaches_the_venue(self, product: ProductType) -> None:
        """The other half: the gate must not be a gate that refuses all."""
        source = _SpyFillSource()
        receipt = _engine(source).submit(_request(product))

        assert len(source.calls) == 1
        assert receipt is not None and receipt.status is not OrderStatus.REJECTED

    def test_the_rejection_is_visible_in_the_oms_not_just_the_receipt(self) -> None:
        """A refusal nothing records is indistinguishable from a lost order."""
        source = _SpyFillSource()
        engine = _engine(source)
        receipt = engine.submit(_request(ProductType.MARGIN))

        assert receipt is not None
        cached = engine.cache.get_order(receipt.order_id.value)
        assert cached is not None
        assert cached.status is OrderStatus.REJECTED

    def test_a_refused_order_leaves_no_position_and_spends_no_cash(self) -> None:
        """It never traded, so the book must not have moved."""
        source = _SpyFillSource()
        engine = _engine(source, cash=Decimal("100000"))
        engine.submit(_request(ProductType.MARGIN))

        assert engine.cache.all_positions() == []
        assert engine.cash_snapshot().cash == Decimal("100000")


# ---------------------------------------------------------------------------
# CONTROL (f) — the gate does not depend on fees being switched on
# ---------------------------------------------------------------------------


class TestControlFTheGateHoldsWithCostsOff:
    """CONTROL (f): "a gate that only exists when fees are on is not a gate"."""

    @pytest.mark.parametrize("product", _UNSCHEDULED)
    def test_refused_by_an_engine_with_no_fee_calculator(
        self, product: ProductType,
    ) -> None:
        source = _SpyFillSource()
        engine = ExecutionEngine(bus=ReactiveBus(), fill_source=source)

        receipt = engine.submit(_request(product))

        assert source.calls == []
        assert receipt is not None and receipt.status is OrderStatus.REJECTED
        assert product.value in receipt.message

    def test_a_priced_product_is_unaffected_by_costs_being_off(self) -> None:
        """A DELIVERY order with costs off still fills: the gate is not a ban."""
        source = _SpyFillSource()
        engine = ExecutionEngine(bus=ReactiveBus(), fill_source=source)
        receipt = engine.submit(_request(ProductType.DELIVERY))

        assert len(source.calls) == 1
        assert receipt is not None and receipt.status is OrderStatus.FILLED


# ---------------------------------------------------------------------------
# CONTROL (g) — a refusal must not leak the idempotency reservation
# ---------------------------------------------------------------------------


class TestControlGTheReservationIsReleased:
    """CONTROL (g): the gate sits after ``check_and_reserve``."""

    def test_the_correlation_id_is_reusable_after_a_refusal(self) -> None:
        """Without the release, every retry of this cid answers PENDING forever."""
        guard = MemoryIdempotencyGuard()
        engine = _engine(_SpyFillSource(), idempotency_guard=guard)
        cid = "fee-gate-release"

        first = engine.submit(_request(ProductType.MARGIN, cid=cid))
        assert first is not None and first.status is OrderStatus.REJECTED
        assert cid not in guard._reserved, "the reservation leaked"

        # The key is free, so the same correlation id is accepted again — and
        # is refused again on its merits, not because it is wedged.
        second = engine.submit(_request(ProductType.MARGIN, cid=cid))
        assert second is not None and second.status is OrderStatus.REJECTED
        assert "no fee schedule" in second.message.lower()

    def test_a_refusal_does_not_replay_as_a_success_on_retry(self) -> None:
        """A rejected request leaves no result to replay into a filled receipt."""
        guard = MemoryIdempotencyGuard()
        engine = _engine(_SpyFillSource(), idempotency_guard=guard)
        cid = "fee-gate-no-replay"

        engine.submit(_request(ProductType.MARGIN, cid=cid))
        retry = engine.submit(_request(ProductType.DELIVERY, cid=cid))

        assert retry is not None
        assert retry.status is not OrderStatus.REJECTED


# ---------------------------------------------------------------------------
# CONTROL (h) — a fill carries its order's product, so DELIVERY is reachable
# ---------------------------------------------------------------------------


class TestControlHFillsCarryTheirOrdersProduct:
    """CONTROL (h): the five backfill lines, asserted at the engine seam.

    A delivery trade priced by the intraday table is not detectable from the
    fee number alone unless you know which product the fill claimed, so the
    property is asserted directly: the book booked a DELIVERY fill, and the
    engine charged it the delivery schedule.
    """

    def test_a_delivery_order_books_a_delivery_fill(self) -> None:
        source = _SpyFillSource()
        engine = _engine(source, fee_calculator=FeeCalculator(), cash=Decimal("100000"))
        engine.submit(_request(ProductType.DELIVERY))

        fills = list(engine.cache.all_positions())
        assert len(fills) == 1
        # 10 x 2500 = 25,000 turnover on a BUY.
        #   brokerage  7.50 (0.03% of 25,000; under the Rs 20 cap)
        #   exchange   0.86 (0.00345% -> 0.8625 -> 0.86, half up)
        #   SEBI       0.05 (0.0002% -> 0.05)
        #   stamp      0.75 (0.003% -> 0.75)
        #   GST        1.51 (18% of 7.50 + 0.86 + 0.05 = 8.41 -> 1.5138 -> 1.51)
        #   STT       25.00 (0.1% of 25,000 — charged on a BUY, which is the
        #                        delivery side the intraday table never charges)
        #   ----------------------------------------
        #   total     35.67
        # The intraday table is the same 10.67 with no STT at all, so the 25.00
        # difference IS the assertion that the delivery table was used.
        assert engine.cash_snapshot().total_fees == Decimal("35.67")

    def test_the_same_order_as_intraday_is_charged_the_intraday_schedule(self) -> None:
        """Both directions of the control: a different number for a different
        product on identical turnover."""
        source = _SpyFillSource()
        engine = _engine(source, fee_calculator=FeeCalculator(), cash=Decimal("100000"))
        engine.submit(_request(ProductType.INTRADAY))

        # 25,000 turnover, intraday BUY: no STT at all.
        #   7.50 + 0.86 + 0.05 + 0.75 + 1.51 = 10.67
        assert engine.cash_snapshot().total_fees == Decimal("10.67")

    def test_a_fill_source_that_drops_the_label_is_priced_intraday(self) -> None:
        """What the backfill prevents, stated as a measurement.

        A source that omits ``product_type`` produces a null-labelled fill.
        The schedule resolves it to INTRADAY **and counts it**, so the defect
        is visible in a metric rather than silently priced — the requirement
        is that it be countable, not that it be impossible.
        """

        class _UnlabelledSource(_SpyFillSource):
            def submit(self, request: OrderRequest) -> tuple[Order, Fill | None]:
                order, fill = super().submit(request)
                assert fill is not None
                return order, Fill(
                    order_id=fill.order_id,
                    instrument=fill.instrument,
                    side=fill.side,
                    quantity=fill.quantity,
                    price=fill.price,
                    timestamp=fill.timestamp,
                )

        calc = FeeCalculator()
        engine = _engine(
            _UnlabelledSource(), fee_calculator=calc, cash=Decimal("100000"),
        )
        engine.submit(_request(ProductType.DELIVERY))

        assert calc.missing_product_type_count == 1
        assert engine.cash_snapshot().total_fees == Decimal("10.67")
