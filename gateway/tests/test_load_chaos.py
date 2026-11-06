"""CI-safe randomized load, chaos, multiplexing and degraded-startup tests."""

from __future__ import annotations

import asyncio

from load_harness import (
    run_consumer_rate_limit,
    run_degraded_startup,
    run_hub_multiplexing,
    run_load,
)


def test_randomized_load_is_fault_tolerant_and_typed() -> None:
    report = asyncio.run(run_load(requests=300, concurrency=20, seed=1337, fail_rate=0.20))
    assert report["completed"] == 300
    assert report["transport_errors"] == 0
    assert report["statuses"].get("500", 0) == 0
    assert report["successes"] > 0
    assert report["brokers"]["dhan"]["calls"] > 0
    assert report["brokers"]["upstox"]["calls"] > 0
    assert (report["brokers"]["dhan"]["failures"] + report["brokers"]["upstox"]["failures"]) > 0
    assert report["duration_seconds"] < 120


def test_consumer_rate_limit_is_enforced() -> None:
    report = asyncio.run(run_consumer_rate_limit(seed=7))
    assert report["first_status"] == 200
    assert report["second_status"] == 429
    assert report["second_code"] == "consumer_rate_limited"
    assert report["retry_after"] is not None


def test_live_hub_multiplexes_many_clients_onto_one_subscription() -> None:
    report = asyncio.run(run_hub_multiplexing(clients=50, instruments=9, seed=7))
    assert report["upstream_subscribe_calls"] == 1
    assert report["upstox_subscribe_calls"] == 0
    assert report["clients_fanned_out"] == 50
    assert report["unsubscribed"] >= 9
    assert report["status_after_unsubscribe"] == "halted"


def test_degraded_startup_serves_from_the_remaining_broker() -> None:
    report = asyncio.run(run_degraded_startup(seed=11))
    assert report["ready"] is True
    assert report["quote_status"] == 200
    assert report["quote_broker"] == "upstox"
