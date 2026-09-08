"""Tests for the shared broker-client plumbing in common/client_shared.

These helpers were duplicated verbatim in the Dhan and Upstox clients; this
file is the single test home for the deduplicated implementations.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from support.fetch_pipeline import FetchResiliencePipeline
from tradex_domain.value_objects import CorrelationId

from tradex_brokers.common.client_shared import (
    build_provider_client,
    correlation_id,
    order_result_from_dict,
    parse_timestamp_fallback,
)


class TestParseTimestampFallback:
    def test_none_returns_fallback(self) -> None:
        fb = datetime(2026, 1, 1, tzinfo=UTC)
        assert parse_timestamp_fallback(None, fb) is fb

    def test_empty_returns_fallback(self) -> None:
        fb = datetime(2026, 1, 1, tzinfo=UTC)
        assert parse_timestamp_fallback("", fb) is fb

    def test_valid_iso_parses(self) -> None:
        fb = datetime(2026, 1, 1, tzinfo=UTC)
        result = parse_timestamp_fallback("2026-08-05T10:00:00+00:00", fb)
        assert result.year == 2026
        assert result.month == 8

    def test_unparseable_returns_fallback(self) -> None:
        fb = datetime(2026, 1, 1, tzinfo=UTC)
        assert parse_timestamp_fallback("not-a-date", fb) is fb


class TestCorrelationId:
    def test_valid_uuid_preserved(self) -> None:
        cid = correlation_id("12345678-1234-5678-1234-567812345678", fallback_seed="x")
        assert isinstance(cid, CorrelationId)
        assert str(cid.value) == "12345678-1234-5678-1234-567812345678"

    def test_invalid_falls_back_deterministic(self) -> None:
        cid1 = correlation_id("garbage", fallback_seed="seed-1")
        cid2 = correlation_id("garbage", fallback_seed="seed-1")
        assert cid1 == cid2

    def test_non_uuid_text_preserved_verbatim(self) -> None:
        """A broker echoes the exact id we sent — never hash it away.

        The strategy bridge emits non-UUID ``strat-...`` ids; the live-fill
        bridge matches broker rows on correlation-id equality, so the echoed
        value must survive the mapper unchanged.
        """
        cid = correlation_id("strat-abc123", fallback_seed="x")
        assert cid.value == "strat-abc123"

    def test_different_text_falls_back_differently(self) -> None:
        assert correlation_id("garbage", fallback_seed="x") != correlation_id(
            "other", fallback_seed="x"
        )

    def test_empty_uses_seed(self) -> None:
        cid = correlation_id(None, fallback_seed="seed-only")
        assert isinstance(cid, CorrelationId)
        assert cid.value is not None


class TestOrderResultFromDict:
    def test_accepts_dhan_super_and_forever_order_ids(self) -> None:
        super_result = order_result_from_dict({"superOrderId": "SO-1", "status": "SUCCESS"})
        forever_result = order_result_from_dict({"foreverOrderId": "FO-1", "status": "SUCCESS"})

        assert super_result.order_id.value == "SO-1"
        assert forever_result.order_id.value == "FO-1"


class TestFetchResiliencePipeline:
    def test_passes_through_dict(self) -> None:
        pipeline = FetchResiliencePipeline(lambda method, url, **kw: {"data": 1})
        result = pipeline.send("GET", "https://x")
        assert result == {"data": 1}

    def test_wraps_non_dict_body(self) -> None:
        pipeline = FetchResiliencePipeline(lambda method, url, **kw: [1, 2])
        assert pipeline.send("GET", "https://x") == {"data": [1, 2]}

    def test_embeds_status_from_tuple(self) -> None:
        pipeline = FetchResiliencePipeline(lambda method, url, **kw: (200, {"ok": True}))
        result = pipeline.send("GET", "https://x")
        assert result["_http_status"] == 200
        assert result["ok"] is True

    def test_embeds_status_for_non_dict_body(self) -> None:
        pipeline = FetchResiliencePipeline(lambda method, url, **kw: (401, "denied"))
        result = pipeline.send("GET", "https://x")
        assert result["_http_status"] == 401
        assert result["data"] == "denied"


class TestBuildProviderClient:
    def test_returns_http_and_ws_token_provider(self) -> None:
        http, ws = build_provider_client(
            fetch=lambda method, url, **kw: {"data": {}},
            base_url="https://api.example.com/v2",
            auth_headers=lambda token: {"Authorization": f"Bearer {token}"},
            access_token="tok",
            provider="paper",
        )
        assert http is not None
        assert callable(ws)
        assert ws() == "tok"

    def test_static_auth_headers_are_sent(self) -> None:
        seen: list[dict[str, str]] = []

        def fetch(method: str, url: str, **kw: Any) -> dict:
            seen.append(dict(kw.get("headers") or {}))
            return {"data": {}}

        http, _ = build_provider_client(
            fetch=fetch,
            base_url="https://api.example.com/v2",
            auth_headers=lambda token: {"Authorization": f"Bearer {token}"},
            access_token="static-token",
            provider="paper",
        )
        http.request("GET", "/orders", cache_read=False)
        assert any(h.get("Authorization") == "Bearer static-token" for h in seen)

