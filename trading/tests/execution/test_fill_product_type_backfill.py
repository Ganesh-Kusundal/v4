"""AC-6 — a fill carries the product of the order it came from, at all five
sites that build a ``Fill``.

``Fill.product_type`` is nullable and was added so the fee model can price a
delivery trade differently from an intraday one. A null is resolved to INTRADAY
*and counted* in ``fills_missing_product_type_total`` rather than guessed, which
is the right default — but it made a null invisible in the only place it
mattered. Every producer of a ``Fill`` in the tree left the field unset, so
**every** production fill was null and every one of them was charged the
intraday schedule. A DELIVERY round trip was billed 0.025% sell-side STT where
the regime in force since 1 October 2024 charges 0.1% on both sides.

Five sites, one line each:

======================================  =========================================
``FillModel.make_fill``                 the shared builder every fill source uses
``FillModel.restamp_fill``              replay re-stamping a recorded fill
``SlippageAwareFillSource.submit``      re-prices a fill, so it rebuilds it
``LiveFillBridge._publish``             the live path, from the venue's own order
``PaperBroker._update_position``        the paper book
======================================  =========================================

**The control is the label's survival, not the line's existence.** Each test
asserts the product arrives at the far side of its seam, and each carries the
counterfactual: DELIVERY and INTRADAY are asserted to *differ*, so a test that
passed because the field defaulted to INTRADAY cannot pass here. A DELIVERY
label and an INTRADAY label are the whole content of the change.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    TimeInForce,
)
from tradex_domain.events import OrderFilled
from tradex_domain.execution import Fill, Order, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import (
    CorrelationId,
    Money,
    OrderId,
    Price,
    Quantity,
)
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_model import FillModel
from tradex_execution.fill_sources import BrokerFillSource
from tradex_execution.slippage import (
    FixedSlippageModel,
    SlippageAwareFillSource,
)
from tradex_reactive.bus import ReactiveBus
from tradex_runtime.live_fill_bridge import LiveFillBridge

INSTRUMENT = Equity.of("NSE", "RELIANCE")
BROKER_ORDER_ID = "dhan-order-pt"
_TS = None


def _order(product: ProductType, *, order_id: str = "o-1") -> Order:
    return Order(
        order_id=OrderId(value=order_id),
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.FILLED,
        product_type=product,
    )


def _fill(product: ProductType | None, *, order_id: str = "o-1") -> Fill:
    return Fill(
        order_id=OrderId(value=order_id),
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        product_type=product,
    )


def _filled_fields(product: ProductType, order_id: str) -> dict:
    """Constructor kwargs for a FILLED order — the shape the paper book
    projects a position from."""
    return {
        "order_id": OrderId(value=order_id),
        "instrument": INSTRUMENT,
        "side": OrderSide.BUY,
        "order_type": OrderType.MARKET,
        "quantity": Quantity(value=Decimal("10")),
        "price": Price(value=Decimal("2500")),
        "time_in_force": TimeInForce.DAY,
        "status": OrderStatus.FILLED,
        "product_type": product,
        "filled_quantity": Quantity(value=Decimal("10")),
    }


# ---------------------------------------------------------------------------
# 1 & 2 — FillModel, the shared builder and the re-stamp
# ---------------------------------------------------------------------------


class TestFillModelCarriesTheOrderProduct:
    def test_make_fill_takes_the_orders_product(self) -> None:
        fill = FillModel().make_fill(
            _order(ProductType.DELIVERY), Price(value=Decimal("2500")), _TS,  # type: ignore[arg-type]
        )
        assert fill.product_type is ProductType.DELIVERY

    def test_make_fill_does_not_collapse_every_product_to_intraday(self) -> None:
        """The counterfactual: the two priced products must not read the same.

        Without this pair a default of INTRADAY would satisfy the test above.
        """
        model = FillModel()
        labels = {
            model.make_fill(
                _order(p), Price(value=Decimal("2500")), _TS,  # type: ignore[arg-type]
            ).product_type
            for p in (ProductType.DELIVERY, ProductType.INTRADAY)
        }
        assert labels == {ProductType.DELIVERY, ProductType.INTRADAY}

    def test_restamp_preserves_a_recorded_products_label(self) -> None:
        """Replay re-stamps recorded fills; a re-stamp must not re-price them."""
        restamped = FillModel.restamp_fill(
            _fill(ProductType.DELIVERY), OrderId(value="replayed"),
        )
        assert restamped.product_type is ProductType.DELIVERY

    def test_restamp_does_not_invent_a_label_it_was_not_given(self) -> None:
        """Honest about the null: re-stamping cannot recover a dropped label, so
        the null survives and the counter still sees it."""
        restamped = FillModel.restamp_fill(
            _fill(None), OrderId(value="replayed"),
        )
        assert restamped.product_type is None


# ---------------------------------------------------------------------------
# 3 — SlippageAwareFillSource, which rebuilds the fill to re-price it
# ---------------------------------------------------------------------------


class _LabelledSource:
    """Inner fill source returning a fill whose product is set by the request."""

    reaches_venue = False
    position_projection_owned = False

    def submit(self, request: OrderRequest) -> tuple[Order, Fill | None]:
        return _order(request.product_type), _fill(request.product_type)

    def cancel(self, order_id: OrderId) -> None:
        pass

    def modify(self, order_id: OrderId, request: OrderRequest) -> None:
        pass


class TestSlippageWrapperPreservesTheProduct:
    def test_the_repriced_fill_keeps_its_product(self) -> None:
        source = SlippageAwareFillSource(
            _LabelledSource(), FixedSlippageModel(Decimal("1")),
        )
        request = OrderRequest(
            instrument=INSTRUMENT,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("2500")),
            time_in_force=TimeInForce.DAY,
            product_type=ProductType.DELIVERY,
        )
        _order_out, fill = source.submit(request)
        assert fill is not None
        assert fill.price.value == Decimal("2501"), "the wrapper did re-price it"
        assert fill.product_type is ProductType.DELIVERY


# ---------------------------------------------------------------------------
# 4 — LiveFillBridge, the live path
# ---------------------------------------------------------------------------


class _AckBroker:
    owns_position_projection = False

    def submit_order(self, request: OrderRequest) -> object:
        return BROKER_ORDER_ID


class _NoopDisposable:
    def dispose(self) -> None:
        pass


class _OrderStream:
    def __init__(self) -> None:
        self._handler = None

    def subscribe_orders(self, handler) -> object:
        self._handler = handler
        return _NoopDisposable()

    def emit(self, order: Order) -> None:
        if self._handler is not None:
            self._handler(order)


def _live_request(product: ProductType, cid: str) -> OrderRequest:
    return OrderRequest(
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        time_in_force=TimeInForce.DAY,
        product_type=product,
        correlation_id=CorrelationId(value=cid),
    )


def _stream_order(product: ProductType, cid: str, *, filled: int) -> Order:
    return Order(
        order_id=OrderId(value=BROKER_ORDER_ID),
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.PARTIALLY_FILLED,
        correlation_id=CorrelationId(value=cid),
        filled_quantity=Quantity(value=Decimal(filled)),
        product_type=product,
    )


class TestLiveBridgeLabelsTheFillFromTheVenueOrder:
    @pytest.mark.parametrize(
        ("product", "cid"),
        [
            (ProductType.DELIVERY, "pt-delivery"),
            (ProductType.INTRADAY, "pt-intraday"),
        ],
    )
    def test_the_published_fill_carries_the_orders_product(
        self, product: ProductType, cid: str,
    ) -> None:
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, BrokerFillSource(_AckBroker()))
        stream = _OrderStream()
        LiveFillBridge(bus, engine, stream.subscribe_orders)
        published: list[OrderFilled] = []
        bus.of_type(OrderFilled).subscribe(published.append)
        try:
            engine.submit(_live_request(product, cid))
            stream.emit(_stream_order(product, cid, filled=3))
            assert published, "the bridge published no fill"
            assert [e.fill.product_type for e in published] == [product]
        finally:
            engine.shutdown()


# ---------------------------------------------------------------------------
# 5 — PaperBroker, the paper book
# ---------------------------------------------------------------------------


class TestPaperBrokerLabelsItsProjectionFill:
    def test_the_fill_the_paper_book_projects_carries_the_product(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The Fill is local to ``_update_position``, so it is captured at the
        position-math call rather than read off a return value."""
        from tradex_brokers.paper import adapter as paper_adapter
        from tradex_brokers.paper.adapter import PaperBroker

        seen: list[Fill] = []
        real = paper_adapter._domain_apply_fill

        def _capture(current, fill):  # type: ignore[no-untyped-def]
            seen.append(fill)
            return real(current, fill)

        monkeypatch.setattr(paper_adapter, "_domain_apply_fill", _capture)

        broker = PaperBroker(starting_cash=Decimal("1000000"))
        try:
            broker._update_position(
                Order(**_filled_fields(ProductType.DELIVERY, "paper-1"))
            )
        finally:
            broker.close()

        assert seen, "the paper book never projected a position"
        assert seen[0].product_type is ProductType.DELIVERY

    def test_the_paper_book_would_have_priced_a_delivery_fill_as_intraday(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The counterfactual for the same seam: the two products must differ on
        the fill the paper book builds, or the label on it is not load-bearing."""
        from tradex_brokers.paper import adapter as paper_adapter
        from tradex_brokers.paper.adapter import PaperBroker
        from tradex_execution.fees import FeeCalculator

        def _built(product: ProductType) -> Fill:
            seen: list[Fill] = []
            real = paper_adapter._domain_apply_fill

            def _capture(current, fill):  # type: ignore[no-untyped-def]
                seen.append(fill)
                return real(current, fill)

            monkeypatch.setattr(paper_adapter, "_domain_apply_fill", _capture)
            broker = PaperBroker(starting_cash=Decimal("1000000"))
            try:
                broker._update_position(Order(**_filled_fields(product, "p")))
            finally:
                broker.close()
            return seen[0]

        calc = FeeCalculator()
        delivery = calc.calculate(_built(ProductType.DELIVERY))
        intraday = calc.calculate(_built(ProductType.INTRADAY))
        assert delivery.amount > intraday.amount


# ---------------------------------------------------------------------------
# The reason the backfill exists, stated as a measurement
# ---------------------------------------------------------------------------


class TestAMissingLabelIsCountedNotGuessed:
    def test_an_unlabelled_fill_is_priced_intraday_and_counted(self) -> None:
        """The invariant the backfill restores is that a *producer* may forget
        the label but the *pricer* never guesses: the null is visible."""
        from tradex_execution.fees import FeeCalculator

        calc = FeeCalculator()
        turnover_fill = _fill(None)
        assert turnover_fill.product_type is None
        fee = calc.calculate(turnover_fill)

        delivery = calc.calculate(_fill(ProductType.DELIVERY))
        intraday = calc.calculate(_fill(ProductType.INTRADAY))

        assert calc.missing_product_type_count == 1
        # The null was priced as INTRADAY, not as zero and not as delivery.
        assert fee == intraday
        assert isinstance(fee, Money)
        # And the two priced products genuinely differ on a BUY, which is why
        # a null label was never harmless: delivery STT is charged on the buy.
        assert delivery.amount > intraday.amount
