from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from gateway_support import FakeBroker, gateway_config, index as make_index
from tradex_brokers.dhan._marketdata import _dhan_leg
from tradex_brokers.upstox._marketdata import _upstox_leg
from tradex_domain.errors import (
    AuthenticationError,
    ConnectionTimeoutError,
    RateLimitError,
)
from tradex_domain.instruments import Option
from tradex_domain.market import Quote
from tradex_domain.options import Expiry, OptionChain, OptionPair
from tradex_domain.value_objects import Price, Quantity
from tradex_gateway.app import create_app
from tradex_gateway.brokers import BrokerRuntime


def _sample_chain(expiry_date: date, ce_delta: float = 0.54) -> OptionChain:
    underlying = make_index("NIFTY")
    strike = Decimal("23500")
    call = Option.of("NFO", "NIFTY", expiry_date, strike, "CE")
    put = Option.of("NFO", "NIFTY", expiry_date, strike, "PE")
    pair = OptionPair(
        call=call,
        put=put,
        strike=Price(value=strike),
        call_leg={
            "ltp": 146.99,
            "oi": 1_250_000.0,
            "volume": 450_000.0,
            "iv": 12.5,
            "greeks": {"delta": ce_delta, "gamma": 0.00132, "theta": -15.15, "vega": 12.18},
            "provider": "dhan",
        },
        put_leg={
            "ltp": 110.50,
            "oi": 980_000.0,
            "volume": 320_000.0,
            "iv": 11.2,
            "greeks": {"delta": -0.46, "gamma": 0.00128, "theta": -14.22, "vega": 11.94},
            "provider": "dhan",
        },
    )
    return OptionChain(
        underlying=underlying,
        _expiries=(
            Expiry(
                underlying=underlying,
                expiry_date=expiry_date,
                pairs=(pair,),
                reference_price=Price(value=Decimal("23510")),
            ),
        ),
    )


def test_dhan_and_upstox_leg_extractors():
    dhan_leg = _dhan_leg(
        {
            "security_id": 12345,
            "last_price": 146.99,
            "oi": 1250000,
            "volume": 450000,
            "previous_oi": 1200000,
            "previous_volume": 400000,
            "top_bid_price": 146.9,
            "top_ask_price": 147.0,
            "top_bid_quantity": 50,
            "top_ask_quantity": 75,
            "implied_volatility": 12.5,
            "greeks": {"delta": 0.5387, "gamma": 0.00132, "theta": -15.15, "vega": 12.18},
        }
    )
    assert dhan_leg == {
        "ltp": 146.99,
        "oi": 1250000.0,
        "volume": 450000.0,
        "previous_oi": 1200000.0,
        "previous_volume": 400000.0,
        "bid": 146.9,
        "ask": 147.0,
        "bid_qty": 50.0,
        "ask_qty": 75.0,
        "iv": 12.5,
        "greeks": {"delta": 0.5387, "gamma": 0.00132, "theta": -15.15, "vega": 12.18},
        "provider": "dhan",
    }

    upstox_leg = _upstox_leg(
        {
            "instrument_key": "NSE_FO|43885",
            "market_data": {
                "ltp": 110.5,
                "oi": 980000,
                "volume": 320000,
                "prev_oi": 950000,
                "bid_price": 110.4,
                "ask_price": 110.6,
                "bid_qty": 120,
                "ask_qty": 90,
            },
            "option_greeks": {
                "delta": -0.46,
                "gamma": 0.00128,
                "theta": -14.22,
                "vega": 11.94,
                "iv": 11.2,
                "pop": 0.62,
            },
        }
    )
    assert upstox_leg == {
        "ltp": 110.5,
        "oi": 980000.0,
        "volume": 320000.0,
        "previous_oi": 950000.0,
        "bid": 110.4,
        "ask": 110.6,
        "bid_qty": 120.0,
        "ask_qty": 90.0,
        "iv": 11.2,
        "greeks": {"delta": -0.46, "gamma": 0.00128, "theta": -14.22, "vega": 11.94},
        "pop": 0.62,
        "provider": "upstox",
    }
    assert "previous_volume" not in upstox_leg
    assert _dhan_leg({}) is None
    assert _upstox_leg(None) is None


def test_gateway_option_chain_and_quote_serialize_greeks_and_oi():
    dhan, upstox = FakeBroker("dhan"), FakeBroker("upstox")
    expiry = date.today() + timedelta(days=7)
    dhan.get_option_chain = lambda _u, _e=None, **_kw: _sample_chain(expiry)
    upstox.get_option_chain = lambda _u, _e=None, **_kw: _sample_chain(expiry, ce_delta=0.55)

    opt = Option.of("NFO", "NIFTY", expiry, Decimal("23500"), "CE")
    upstox.quote_value = Quote(
        instrument=opt,
        ltp=Price(value=Decimal("147.25")),
        bid=Price(value=Decimal("147.00")),
        ask=Price(value=Decimal("147.50")),
        volume=Quantity(value=Decimal("450000")),
        metadata={"greeks": {"delta": 0.54, "gamma": 0.0013, "theta": -15.1, "vega": 12.2}},
        timestamp=datetime.now(UTC),
    )

    app = create_app(
        gateway_config(allow_anon=True),
        {"dhan": BrokerRuntime("dhan", dhan), "upstox": BrokerRuntime("upstox", upstox)},
    )
    headers = {"X-API-Key": "test-key"}
    with TestClient(app) as client:
        res = client.get(f"/v1/option-chain/IDX:NIFTY?expiry={expiry.isoformat()}", headers=headers)
        assert res.status_code == 200
        body = res.json()
        assert body["broker"] == "dhan"
        ce = body["strikes"][0]["ce"]
        pe = body["strikes"][0]["pe"]
        assert ce["leg"]["ltp"] == 146.99
        assert ce["leg"]["oi"] == 1_250_000.0
        assert ce["leg"]["volume"] == 450_000.0
        assert ce["leg"]["iv"] == 12.5
        assert ce["leg"]["greeks"] == {"delta": 0.54, "gamma": 0.00132, "theta": -15.15, "vega": 12.18}
        assert ce["leg"]["provider"] == "dhan"
        assert pe["leg"]["greeks"]["delta"] == -0.46

        qres = client.get(f"/v1/quote/{opt.instrument_id}", headers=headers)
        assert qres.status_code == 200
        assert qres.json()["greeks"] == {"delta": 0.54, "gamma": 0.0013, "theta": -15.1, "vega": 12.2}


@pytest.mark.parametrize(
    "error_factory,expected_cooldown",
    [
        (lambda: AuthenticationError("401 invalid token"), 60.0),
        (lambda: RateLimitError("429 rate limit"), 30.0),
        (lambda: ConnectionTimeoutError("gateway timeout"), 15.0),
    ],
)
def test_gateway_option_chain_fails_over_and_trips_cooldown(error_factory, expected_cooldown):
    dhan, upstox = FakeBroker("dhan"), FakeBroker("upstox")
    expiry = date.today() + timedelta(days=7)

    def failing_dhan(_u, _e=None, *, allow_master_fallback: bool = True):
        assert allow_master_fallback is False
        raise error_factory()

    dhan.get_option_chain = failing_dhan
    upstox.get_option_chain = lambda _u, _e=None, **_kw: _sample_chain(expiry, ce_delta=0.51)

    now = [1000.0]
    app = create_app(
        gateway_config(allow_anon=True),
        {
            "dhan": BrokerRuntime("dhan", dhan, clock=lambda: now[0]),
            "upstox": BrokerRuntime("upstox", upstox, clock=lambda: now[0]),
        },
    )
    headers = {"X-API-Key": "test-key"}
    with TestClient(app) as client:
        app.state.router._clock = lambda: now[0]
        res = client.get(f"/v1/option-chain/IDX:NIFTY?expiry={expiry.isoformat()}", headers=headers)
        assert res.status_code == 200
        body = res.json()
        assert body["broker"] == "upstox"
        assert body["strikes"][0]["ce"]["leg"]["greeks"]["delta"] == 0.51

        health = client.get("/v1/health", headers=headers).json()
        dhan_chain = next(b for b in health["brokers"] if b["name"] == "dhan")["health"]["chain"]
        assert dhan_chain["available"] is False
        assert dhan_chain["cooldown_seconds"] == expected_cooldown

        # After cooldown expires, dhan becomes eligible again
        now[0] += expected_cooldown + 1.0
        health_after = client.get("/v1/health", headers=headers).json()
        dhan_chain_after = next(b for b in health_after["brokers"] if b["name"] == "dhan")["health"]["chain"]
        assert dhan_chain_after["available"] is True
