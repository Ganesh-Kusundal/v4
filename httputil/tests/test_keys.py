from __future__ import annotations

import pytest
from tradex_http import keys_match


def test_match() -> None:
    assert keys_match("secret", "secret") is True


def test_mismatch() -> None:
    assert keys_match("wrong", "secret") is False


@pytest.mark.parametrize("provided,expected", [(None, "k"), ("k", None), ("", ""), ("", "k")])
def test_none_and_empty_never_match(provided: str | None, expected: str | None) -> None:
    assert keys_match(provided, expected) is False


def test_non_ascii_returns_false_not_raises() -> None:
    assert keys_match("é", "secret") is False


def test_non_ascii_equal_matches() -> None:
    assert keys_match("é", "é") is True
