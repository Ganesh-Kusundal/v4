"""Tests for correlation_id."""
import hashlib

from tradex_domain.correlation_id import mint_correlation_id


def test_mint_is_deterministic():
    assert mint_correlation_id("strat", 1) == mint_correlation_id("strat", 1)


def test_mint_includes_prefix():
    cid = mint_correlation_id("strat", 1)
    assert cid.startswith("strat-")


def test_mint_is_hex_digest():
    cid = mint_correlation_id("strat", 1)
    digest = cid.split("-", 1)[1]
    int(digest, 16)


def test_mint_changes_with_n():
    assert mint_correlation_id("strat", 1) != mint_correlation_id("strat", 2)


def test_mint_changes_with_prefix():
    assert mint_correlation_id("strat", 1) != mint_correlation_id("bt", 1)


def test_mint_matches_sha256():
    prefix, n = "strat", 42
    expected = hashlib.sha256(f"{prefix}:{n}".encode()).hexdigest()[:16]
    assert mint_correlation_id(prefix, n) == f"strat-{expected}"


def test_mint_handles_zero():
    cid = mint_correlation_id("x", 0)
    assert cid.startswith("x-")
    int(cid.split("-", 1)[1], 16)
