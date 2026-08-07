"""Tests for BrokerFactory registry: register/create, unregistered, and type validation."""

from __future__ import annotations

import pytest
from tradex_domain import BrokerId, BrokerUnavailableError

from tradex_brokers.registry import BrokerFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _DummyAdapter:
    """Minimal broker adapter for testing — satisfies the BrokerAdapter protocol."""

    capabilities = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def connect(self) -> None: ...
    def close(self) -> None: ...
    def submit_order(self, request): ...
    def cancel_order(self, order_id): ...
    def modify_order(self, order_id, request): ...
    def get_order(self, order_id): ...
    def get_orderbook(self): ...
    def get_positions(self): ...
    def get_holdings(self): ...
    def get_account(self): ...
    def get_portfolio(self): ...
    def get_quote(self, instrument): ...
    def ltp(self, instrument): ...
    def depth(self, instrument): ...
    def history(self, instrument, timeframe, start, end): ...
    def get_option_chain(self, underlying, expiry=None): ...
    def search(self, query): ...
    def stream_backend(self, **kwargs): ...
    def market_stream_backend(self, **kwargs): ...
    def depth_stream_backend(self, **kwargs): ...
    def subscribe_quotes(self, instruments, handler): ...
    def subscribe_depth(self, instrument, handler): ...
    def unsubscribe(self, subscription): ...
    def load_instruments(self): ...


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBrokerFactory:
    """BrokerFactory registry behaviour."""

    def setup_method(self) -> None:
        """Snapshot the shared registry before each test (restored in teardown)."""
        self._saved_registry = dict(BrokerFactory._registry)
        BrokerFactory.clear()

    def teardown_method(self) -> None:
        """Restore the shared registry so later tests see built-in brokers."""
        BrokerFactory._registry.clear()
        BrokerFactory._registry.update(self._saved_registry)

    def test_factory_register_and_create(self) -> None:
        """Register an adapter and create it successfully."""
        BrokerFactory.register(BrokerId.PAPER, _DummyAdapter)
        instance = BrokerFactory.create(BrokerId.PAPER)
        assert isinstance(instance, _DummyAdapter)

        # With kwargs
        instance2 = BrokerFactory.create(BrokerId.PAPER, api_key="test-key")
        assert instance2.kwargs == {"api_key": "test-key"}

    def test_factory_create_unregistered_raises_unavailable(self) -> None:
        """Creating an unregistered broker raises BrokerUnavailableError."""
        BrokerFactory.clear()
        with pytest.raises(BrokerUnavailableError, match="No adapter registered"):
            BrokerFactory.create(BrokerId.REPLAY)

    def test_factory_rejects_non_broker_id(self) -> None:
        """Registering with a non-BrokerId raises TypeError."""
        with pytest.raises(TypeError, match="BrokerId"):
            BrokerFactory.register("NOT_A_BROKER_ID", _DummyAdapter)
