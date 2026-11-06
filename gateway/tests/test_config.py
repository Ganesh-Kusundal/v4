from __future__ import annotations

from gateway_support import FakeClock
from tradex_gateway.auth import TokenBucket
from tradex_gateway.config import GatewayConfig


def test_from_env_parses_lists_and_numbers():
    config = GatewayConfig.from_env(
        {
            "GATEWAY_HOST": "0.0.0.0",
            "GATEWAY_PORT": "9000",
            "GATEWAY_API_KEY": "secret",
            "GATEWAY_BROKERS": "dhan, upstox",
            "GATEWAY_PRIMARY_HISTORY": "dhan",
            "GATEWAY_REST_RATE_PER_SECOND": "5",
            "GATEWAY_REST_BURST": "7",
        }
    )
    assert config.host == "0.0.0.0"
    assert config.port == 9000
    assert config.brokers == ("dhan", "upstox")
    assert config.primary_history == "dhan"
    assert config.rest_rate_per_second == 5.0
    assert config.rest_burst == 7
    assert config.validate() == config


def test_validate_rejects_unknown_broker():
    try:
        GatewayConfig(api_key="k", brokers=("dhan", "kite")).validate()
    except ValueError as exc:
        assert "kite" in str(exc)
    else:  # pragma: no cover - the assertion above must fail first
        raise AssertionError("validate() accepted an unknown broker")


def test_validate_rejects_non_positive_bounds():
    for field_name in ("broker_workers", "stale_after_seconds", "max_subscriptions"):
        try:
            GatewayConfig(api_key="k", **{field_name: 0}).validate()
        except ValueError as exc:
            assert field_name.upper() in str(exc)
        else:  # pragma: no cover - the assertion above must fail first
            raise AssertionError(f"validate() accepted {field_name}=0")


def test_validate_requires_key_off_loopback():
    config = GatewayConfig(host="0.0.0.0", api_key=None)
    try:
        config.validate()
    except ValueError as exc:
        assert "API key" in str(exc) or "GATEWAY_API_KEY" in str(exc)
    else:  # pragma: no cover - the assertion above must fail first
        raise AssertionError("validate() accepted a non-loopback bind without a key")


def test_validate_normalizes_disabled_primary():
    config = GatewayConfig(api_key="k", brokers=("upstox",), primary_history="dhan").validate()
    assert config.primary_history == "upstox"


def test_token_bucket_refills():
    clock = FakeClock(0.0)
    bucket = TokenBucket(rate=1.0, burst=2, clock=clock)
    assert bucket.consume("k")[0] is True
    assert bucket.consume("k")[0] is True
    allowed, retry_after = bucket.consume("k")
    assert allowed is False
    assert retry_after > 0
    clock.advance(1.0)
    assert bucket.consume("k")[0] is True


def test_token_bucket_evicts_least_recently_used_key():
    clock = FakeClock(0.0)
    bucket = TokenBucket(rate=1.0, burst=1, max_keys=2, clock=clock)
    bucket.consume("a")
    bucket.consume("b")
    bucket.consume("c")
    assert len(bucket._buckets) == 2
    assert "a" not in bucket._buckets


def test_token_bucket_refreshes_recently_used_key():
    clock = FakeClock(0.0)
    bucket = TokenBucket(rate=1.0, burst=1, max_keys=2, clock=clock)
    bucket.consume("a")
    bucket.consume("b")
    bucket.consume("a")
    bucket.consume("c")
    assert set(bucket._buckets) == {"a", "c"}
    assert "b" not in bucket._buckets
