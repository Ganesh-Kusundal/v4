"""Tests for order_id."""
import uuid

from tradex_domain.order_id import mint_order_id


def test_mint_returns_str():
    assert isinstance(mint_order_id(), str)


def test_mint_is_valid_uuid():
    oid = mint_order_id()
    uuid.UUID(oid)


def test_mint_is_unique():
    assert mint_order_id() != mint_order_id()


def test_mint_many_are_unique():
    ids = {mint_order_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_mint_is_canonical_uuid4():
    oid = mint_order_id()
    assert str(uuid.UUID(oid)) == oid
