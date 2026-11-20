"""A non-ASCII CSRF header is a mismatch, not a TypeError."""
from tradex_interfaces.auth.csrf import make_csrf_token, verify_csrf_token


def test_non_ascii_csrf_token_is_rejected_not_raised() -> None:
    assert verify_csrf_token("secret", "é") is False


def test_valid_csrf_token_still_verifies() -> None:
    assert verify_csrf_token("secret", make_csrf_token("secret")) is True
