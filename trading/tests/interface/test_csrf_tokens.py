"""Unit tests for CSRF token generation and verification — B8."""
from __future__ import annotations

import secrets

from tradex_trading.interface.auth.csrf import make_csrf_token, verify_csrf_token


def test_make_csrf_token_returns_hex_string() -> None:
    token = make_csrf_token("some-secret")
    assert isinstance(token, str)
    assert len(token) == 64  # SHA-256 hex


def test_make_csrf_token_is_deterministic() -> None:
    secret = secrets.token_hex(32)
    assert make_csrf_token(secret) == make_csrf_token(secret)


def test_verify_csrf_token_accepts_correct_token() -> None:
    secret = secrets.token_hex(32)
    token = make_csrf_token(secret)
    assert verify_csrf_token(secret, token) is True


def test_verify_csrf_token_rejects_wrong_token() -> None:
    secret = secrets.token_hex(32)
    assert verify_csrf_token(secret, "wrong") is False


def test_verify_csrf_token_rejects_empty_token() -> None:
    assert verify_csrf_token(secrets.token_hex(32), "") is False


def test_tokens_differ_across_secrets() -> None:
    t1 = make_csrf_token(secrets.token_hex(32))
    t2 = make_csrf_token(secrets.token_hex(32))
    assert t1 != t2
