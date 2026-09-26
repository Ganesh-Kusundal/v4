"""Contract: the signed-quantity money path keys off ``OrderSide`` identity.

``apply_fill`` is the single authoritative fill applier (parity CRITICAL-1), so
a wrong answer here silently flips position direction rather than raising. The
side comparison is therefore ``fill.side is OrderSide.BUY`` — identity, not
string equality.

Why identity rather than ``.value == "BUY"``: ``OrderSide`` is a ``StrEnum``,
and ``Fill`` is a plain frozen dataclass whose ``side`` annotation is not
enforced at runtime. A value that merely *looks* like a buy — a plain object
exposing ``.value == "BUY"``, or a second ``StrEnum`` that reuses the string —
previously passed a ``== "BUY"`` check and was booked as a real BUY. Under
identity, only the enum member itself is a BUY; anything else is not a BUY and
is treated as a SELL.

These tests pin both halves of that contract:

* the typo/lookalike failure mode is no longer reachable, and
* genuine ``OrderSide.BUY`` / ``OrderSide.SELL`` behaviour is unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill
from tradex_domain.instruments import Equity
from tradex_domain.position_math import apply_fill
from tradex_domain.value_objects import OrderId, Price, Quantity

_TS = datetime(2026, 8, 1, tzinfo=UTC)


class _ForeignSide(StrEnum):
    """A *different* StrEnum that reuses the same wire values.

    ``_ForeignSide.BUY == "BUY"`` is True, so any comparison written against
    the bare string accepts it — but it is not ``OrderSide.BUY``.
    """

    BUY = "BUY"
    SELL = "SELL"


class _DuckSide:
    """A non-enum object exposing ``.value``, mimicking the enum's shape."""

    def __init__(self, value: str) -> None:
        self.value = value


def _fill(side: object, qty: str, price: str) -> Fill:
    """Build a Fill carrying ``side`` verbatim.

    ``side`` is typed ``object`` on purpose: these tests deliberately smuggle
    values that are not ``OrderSide`` members past the (unenforced) annotation
    to prove the runtime branch keys off identity, not string equality.
    """
    return Fill(
        order_id=OrderId(value="side-oid"),
        instrument=Equity.of("NSE", "SIDEID"),
        side=side,  # type: ignore[arg-type]
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal(price)),
        timestamp=_TS,
    )


class TestSideComparisonIsEnumIdentity:
    """The failure mode the identity check removes."""

    def test_plain_string_buy_is_not_accepted_as_a_buy(self) -> None:
        """A bare ``"BUY"`` is not the enum member, so it is not a BUY.

        A raw string has no ``.value``, so the old ``side.value == "BUY"``
        form raised ``AttributeError`` here. Under identity the branch simply
        reads False and the fill is treated as a SELL — the only way to reach
        the quantity is the sell one.
        """
        pos = apply_fill(None, _fill("BUY", "2", "100"))  # type: ignore[arg-type]
        assert pos.quantity.value == Decimal("-2")

    def test_lookalike_with_buy_value_is_treated_as_a_sell(self) -> None:
        """The silent-flip case: ``.value == "BUY"`` but not the member.

        This is the bug the change actually closes. Under the old string
        comparison this opened a *long* 2 @ 100; it now opens a short, which
        is the conservative direction for an unrecognised side.
        """
        pos = apply_fill(None, _fill(_DuckSide("BUY"), "2", "100"))
        assert pos.quantity.value == Decimal("-2")

    def test_foreign_str_enum_reusing_buy_is_treated_as_a_sell(self) -> None:
        """A different StrEnum with the same value is still not OrderSide.BUY."""
        assert _ForeignSide.BUY == "BUY"  # the lookalike is string-equal
        assert _ForeignSide.BUY is not OrderSide.BUY
        pos = apply_fill(None, _fill(_ForeignSide.BUY, "2", "100"))
        assert pos.quantity.value == Decimal("-2")

    def test_lookalike_sell_matches_genuine_sell_on_open(self) -> None:
        """A lookalike SELL books exactly like the real enum member."""
        expected = apply_fill(None, _fill(OrderSide.SELL, "3", "100"))
        for side in (_DuckSide("SELL"), _ForeignSide.SELL):
            pos = apply_fill(None, _fill(side, "3", "100"))
            assert pos.quantity.value == expected.quantity.value
            assert pos.avg_price.value == expected.avg_price.value

    def test_lookalike_buy_does_not_add_to_an_open_long(self) -> None:
        """The existing-position branch is guarded the same way.

        The old code added the lookalike to a 3-lot long (4 lots, realized 0).
        Now it is read as a SELL that closes the position instead, which is
        the same accounting a genuine ``OrderSide.SELL`` would produce.
        """
        long_pos = apply_fill(None, _fill(OrderSide.BUY, "3", "100"))
        pos = apply_fill(long_pos, _fill(_DuckSide("BUY"), "1", "120"))
        expected = apply_fill(
            apply_fill(None, _fill(OrderSide.BUY, "3", "100")),
            _fill(OrderSide.SELL, "1", "120"),
        )
        assert pos.quantity.value == expected.quantity.value
        assert pos.realized_pnl.amount == expected.realized_pnl.amount


class TestGenuineSidesAreUnchanged:
    """Regression guard — the enum members must keep their exact arithmetic."""

    def test_buy_opens_a_long(self) -> None:
        pos = apply_fill(None, _fill(OrderSide.BUY, "2", "100"))
        assert pos.quantity.value == Decimal("2")
        assert pos.avg_price.value == Decimal("100")
        assert pos.realized_pnl.amount == Decimal("0")

    def test_sell_opens_a_short(self) -> None:
        pos = apply_fill(None, _fill(OrderSide.SELL, "2", "100"))
        assert pos.quantity.value == Decimal("-2")
        assert pos.avg_price.value == Decimal("100")
        assert pos.realized_pnl.amount == Decimal("0")

    def test_buy_weighted_average_unchanged(self) -> None:
        pos = apply_fill(
            apply_fill(None, _fill(OrderSide.BUY, "2", "100")),
            _fill(OrderSide.BUY, "1", "110"),
        )
        assert pos.quantity.value == Decimal("3")
        assert pos.avg_price.value == Decimal("103.33")
        assert pos.realized_pnl.amount == Decimal("0.00")

    def test_sell_partial_close_realizes_unchanged(self) -> None:
        pos = apply_fill(
            apply_fill(None, _fill(OrderSide.BUY, "3", "100")),
            _fill(OrderSide.SELL, "1", "120"),
        )
        assert pos.quantity.value == Decimal("2")
        assert pos.realized_pnl.amount == Decimal("20.00")

    def test_flip_rebase_unchanged(self) -> None:
        pos = apply_fill(
            apply_fill(None, _fill(OrderSide.SELL, "10", "100")),
            _fill(OrderSide.BUY, "20", "90"),
        )
        assert pos.quantity.value == Decimal("10")
        assert pos.avg_price.value == Decimal("90")
        assert pos.realized_pnl.amount == Decimal("100.00")

    def test_strenum_construction_yields_the_identical_member(self) -> None:
        """The wire path (``OrderSide("buy".upper())``) still resolves.

        Parsing untrusted text must keep producing the *member*, which is what
        makes the identity check safe: the coercion happens at the boundary,
        and everything downstream compares identity.
        """
        assert OrderSide("BUY") is OrderSide.BUY
        assert OrderSide("SELL") is OrderSide.SELL
        assert OrderSide("buy".upper()) is OrderSide.BUY
