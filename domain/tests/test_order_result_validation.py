"""Tests for OrderResult domain invariants."""

from __future__ import annotations

import pytest

from tradex_domain.execution import OrderResult
from tradex_domain.value_objects import OrderId


@pytest.mark.parametrize("value", ["", "   "])
def test_order_result_rejects_blank_order_id(value: str) -> None:
    with pytest.raises(ValueError, match="order_id must not be blank"):
        OrderResult(order_id=OrderId(value))
