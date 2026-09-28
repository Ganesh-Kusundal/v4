"""Data reads must be refused without credentials once a key is configured.

Defect (Critical, commit e1c23e6): 39 of 43 HTTP operations carried no auth
dependency. Only the four order mutations were guarded, so anyone who could
reach the port could read positions, holdings, order history, account balance
and margin -- a full read of the account with no credential at all. All 25 data
reads are now behind ``require_auth``.

The invariant pinned here is deliberately stronger than a status-code check.
The pre-existing suite already asserts ``403`` on these paths; asserting the
status again would guard nothing new. What actually matters is the *payload*:
a refusal that still leaks the body is not a refusal. Each test therefore binds
a session holding recognisable data, requests it with no credentials, and
asserts the secret values are absent from the response -- then asserts they are
present once the correct ``X-API-Key`` is supplied. Both halves are needed: the
first proves no leak, the second proves the guard is not simply refusing
everyone (which is the failure mode of a test that never sends a key).

``require_auth`` (interfaces/src/tradex_interfaces/auth/deps.py) is the
dependency under test. It resolves a session cookie first, then the legacy
``X-API-Key`` header, and is *open* when no key is configured -- the paper/dev
posture the last test pins so this hardening cannot leak into local use.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient
from tradex_interfaces.fastapi_app import create_app

# Values chosen to be unmissable in a response body if they ever leak.
_SECRET_BALANCE = "31415926.50"
_SECRET_MARGIN = "27182818.25"
_SECRET_EQUITY = "58598744.75"
_SECRET_SYMBOL = "RELIANCE"
_SECRET_QTY = "7"


def _account() -> MagicMock:
    account = MagicMock()
    account.balance = _SECRET_BALANCE
    account.margin = _SECRET_MARGIN
    account.equity = _SECRET_EQUITY
    return account


def _holding() -> MagicMock:
    holding = MagicMock()
    holding.instrument.instrument_id = f"NSE:{_SECRET_SYMBOL}"
    holding.quantity.value = Decimal(_SECRET_QTY)
    return holding


def _position() -> MagicMock:
    """A cached position, the shape ``/positions`` and ``/holdings`` read now.

    Lane 3i moved the portfolio routes off ``broker.get_holdings()`` onto the
    OMS cache (``get_session_portfolio``), because for a paper session the
    broker's own dict is never written by the session's fill path and a filled
    position reported a flat account. ``_holding`` is kept for the auth
    assertions that do not go through the OMS book, and this is the equivalent
    a session-scoped read actually returns.
    """
    position = MagicMock()
    position.instrument.instrument_id = f"NSE:{_SECRET_SYMBOL}"
    position.quantity.value = Decimal(_SECRET_QTY)
    position.avg_price.value = Decimal("0")
    position.realized_pnl.amount = Decimal("0")
    position.unrealized_pnl.amount = Decimal("0")
    position.mark_price = None
    position.marked_at = None
    position.mark_source = None
    position.is_long = True
    position.is_short = False
    return position


def _session_with_data() -> MagicMock:
    """A bound session whose reads return recognisable, secret-bearing data.

    Built on a MagicMock rather than a real ``TradingSession`` for the same
    reason the existing route tests use one: the subject is the auth
    dependency, and a real session would drag in a broker and an OMS for no
    additional signal. ``engine.cache.all_positions`` is stubbed to a list so
    ``/positions`` has a row to return; the two other reads come straight off
    the broker.
    """
    session = MagicMock()
    session.state = "READY"
    session.broker.get_account.return_value = _account()
    session.broker.get_holdings.return_value = [_holding()]
    session.engine.cache.all_positions.return_value = [_position()]
    return session


def _client(api_key: str | None) -> TestClient:
    return TestClient(create_app(session=_session_with_data(), api_key=api_key))


def _body_has_no_secret(response) -> None:
    text = response.text
    for secret in (
        _SECRET_BALANCE,
        _SECRET_MARGIN,
        _SECRET_EQUITY,
        _SECRET_SYMBOL,
        _SECRET_QTY,
    ):
        assert secret not in text, (
            f"an unauthenticated request returned account data ({secret!r} "
            f"present in the body): {text[:400]!r}"
        )


# ---------------------------------------------------------------------------
# The unauthenticated data plane
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/positions", "/holdings", "/account", "/orders"])
def test_data_routes_refuse_unauthenticated_reads(path: str) -> None:
    """No credential, no payload -- the body must not carry the data either."""
    response = _client(api_key="secret-key").get(path)

    assert response.status_code in (401, 403), (
        f"{path} answered {response.status_code} with no credentials; anyone "
        "reaching the port could read the account"
    )
    _body_has_no_secret(response)


@pytest.mark.parametrize(
    "path",
    [
        "/positions",
        "/holdings",
        "/account",
        # Chart data plane: balance, margin, P&L and the order book.
        "/api/charts/account",
        "/api/charts/book",
    ],
)
def test_data_routes_refuse_a_wrong_key_too(path: str) -> None:
    """A wrong key is refused exactly as no key is, and leaks no less."""
    client = _client(api_key="secret-key")
    response = client.get(path, headers={"X-API-Key": "not-the-key"})

    assert response.status_code in (401, 403), f"{path} accepted a wrong key"
    _body_has_no_secret(response)


# ---------------------------------------------------------------------------
# The authorised path still works
# ---------------------------------------------------------------------------


def test_account_is_served_to_a_valid_key() -> None:
    """The guard refuses strangers, not the operator."""
    response = _client(api_key="secret-key").get(
        "/account", headers={"X-API-Key": "secret-key"}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "balance": _SECRET_BALANCE,
        "margin": _SECRET_MARGIN,
        "equity": _SECRET_EQUITY,
    }


def test_holdings_are_served_to_a_valid_key() -> None:
    response = _client(api_key="secret-key").get(
        "/holdings", headers={"X-API-Key": "secret-key"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == [
        {"instrument_id": f"NSE:{_SECRET_SYMBOL}", "quantity": float(_SECRET_QTY)}
    ]


def test_positions_are_served_to_a_valid_key() -> None:
    """``/positions`` now answers the live OMS book, so it carries a row.

    Lane 3i moved the portfolio routes onto ``get_session_portfolio`` (the
    session's OMS cache) instead of the broker's own holdings dict. Pinning
    the populated shape keeps the guard honest across all three shapes — an
    empty list, a populated list, and an object — and stops it from being
    satisfied by a route that silently serves a stub. ``_SECRET_QTY`` appears
    precisely because the row is live data the guard must protect.
    """
    response = _client(api_key="secret-key").get(
        "/positions", headers={"X-API-Key": "secret-key"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 1
    assert body[0]["instrument"] == f"NSE:{_SECRET_SYMBOL}"
    assert body[0]["quantity"] == _SECRET_QTY


# ---------------------------------------------------------------------------
# The boundaries that must not move
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/health", "/health/live", "/health/ready"])
def test_health_probes_stay_open_without_a_key(path: str) -> None:
    """docker HEALTHCHECK and Kubernetes probes cannot present credentials.

    Guarding these would make the container look unhealthy and get it killed,
    which is a worse outage than the one the key protects against.
    """
    assert _client(api_key="secret-key").get(path).status_code == 200


def test_a_keyless_server_is_unaffected() -> None:
    """``require_auth`` is a no-op when no key is configured.

    Paper/dev is the documented open posture; hardening must not turn local
    development into a login wall.
    """
    client = _client(api_key=None)
    for path in ("/positions", "/holdings", "/account"):
        response = client.get(path)
        assert response.status_code not in (401, 403), (
            f"{path} demands credentials on a keyless (paper/dev) server"
        )


def test_an_empty_key_does_not_match_a_configured_one() -> None:
    """``""`` is not a valid credential for a non-empty configured key.

    A header present-but-empty is the classic bypass: naive truthiness checks
    read it as "the client sent something" and let it through.
    """
    client = _client(api_key="secret-key")
    response = client.get("/account", headers={"X-API-Key": ""})

    assert response.status_code in (401, 403), (
        "an empty X-API-Key authenticated against a configured key"
    )
    _body_has_no_secret(response)


def test_auth_is_enforced_before_the_handler_runs() -> None:
    """Refusal must come from the dependency, not from a handler-level check.

    A route that validated inside its body would still execute that body -- and
    still hit the session -- before refusing. The router-level dependency is
    what makes the refusal free of side effects, so this asserts the broker was
    never asked for the data.
    """
    session = _session_with_data()
    client = TestClient(create_app(session=session, api_key="secret-key"))

    client.get("/account")

    session.broker.get_account.assert_not_called(), (
        "the handler ran before the auth dependency refused; the refusal is "
        "supposed to happen before any account data is read"
    )


def test_require_auth_does_not_consult_the_api_key_when_unconfigured() -> None:
    """A misconfigured deployment must fail open only when it is keyless.

    Documented posture, pinned so the fail-closed and fail-open branches cannot
    be swapped without a red test here.
    """
    session: Any = _session_with_data()
    client = TestClient(create_app(session=session, api_key=None))

    assert client.get("/account").status_code == 200
