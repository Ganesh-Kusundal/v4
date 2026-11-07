"""Shared fixtures for the SDK test suite."""

from __future__ import annotations

import pytest


class _TestStrategy:
    """A minimal strategy for tests that boot in live mode."""

    strategy_id = "test_strategy"
    version = "0.0.0"

    def on_bar(self, context, candle):
        return None

    def on_quote(self, context, quote):
        return None

    def on_depth(self, context, depth):
        return None

    def on_fill(self, context, fill):
        return None


@pytest.fixture(autouse=True)
def _register_test_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register ``_TestStrategy`` so live boots may name it."""
    import tradex_runtime.startup as startup

    monkeypatch.setattr(startup, "all_strategies", (_TestStrategy(),))
    monkeypatch.setattr(startup, "all_definitions", ())
