"""A5 — every top-level HTTP response carries the wire-contract version.

A client detects a breaking shape change from ``schema_version`` instead of
discovering it as a missing key at runtime. This locks that in: any response
model added without the version fails here.
"""

from __future__ import annotations

import inspect
import re

from tradex_domain.constants import API_SCHEMA_VERSION
from tradex_interfaces import contracts
from tradex_interfaces.contracts import VersionedResponse


def test_api_schema_version_is_semver() -> None:
    assert re.match(r"^\d+\.\d+\.\d+$", API_SCHEMA_VERSION), API_SCHEMA_VERSION


def test_every_top_level_response_is_versioned() -> None:
    responses = [
        obj
        for obj in vars(contracts).values()
        if inspect.isclass(obj)
        and issubclass(obj, VersionedResponse)
        and obj is not VersionedResponse
    ]
    assert len(responses) >= 15, f"expected many versioned responses, got {len(responses)}"
    for cls in responses:
        field = cls.model_fields.get("schema_version")
        assert field is not None, f"{cls.__name__} lacks schema_version"
        assert field.default == API_SCHEMA_VERSION, cls.__name__


def test_requests_are_not_versioned() -> None:
    # A request has no schema_version — only responses describe their shape.
    assert "schema_version" not in contracts.BacktestRequest.model_fields
    assert "schema_version" not in contracts.PlaceOrderRequest.model_fields
