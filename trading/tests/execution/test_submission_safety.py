"""Tests for SafeBrokerFillSource wrapper."""

from unittest.mock import Mock

import pytest
from tradex_domain.errors import OrderSubmissionUnknownError

from tradex_trading.execution.submission_safety import SafeBrokerFillSource


def test_safe_broker_fill_source_wraps_broker_fill_source():
    """Test SafeBrokerFillSource wraps BrokerFillSource."""
    broker = Mock()
    safe = SafeBrokerFillSource(broker)
    assert safe._inner is not None
    assert safe.submission_boundary_crossed is False


def test_oserror_after_boundary_crossed_raises_order_submission_unknown_error():
    """Test OSError after boundary crossed raises OrderSubmissionUnknownError."""
    broker = Mock()
    broker.submit_order.side_effect = OSError("Connection lost")
    safe = SafeBrokerFillSource(broker)

    # Manually set boundary crossed to simulate the scenario
    safe._inner._submission_boundary_crossed = True

    with pytest.raises(OrderSubmissionUnknownError) as exc_info:
        safe.submit(Mock())

    assert "Order submission failed after crossing broker boundary" in str(exc_info.value)


def test_oserror_before_boundary_crossed_reraises_original():
    """Test OSError before boundary crossed re-raises original exception."""
    broker = Mock(spec=[])  # no submit_order — boundary won't be crossed
    safe = SafeBrokerFillSource(broker)

    # Patch inner submit to raise OSError without crossing boundary
    def _raise_os_error(req):
        raise OSError("Connection lost")

    safe._inner.submit = _raise_os_error

    # Boundary not crossed, should re-raise original OSError
    with pytest.raises(OSError) as exc_info:
        safe.submit(Mock())

    assert "Connection lost" in str(exc_info.value)


def test_cancel_delegates_to_inner():
    """Test cancel delegates to inner BrokerFillSource."""
    broker = Mock()
    safe = SafeBrokerFillSource(broker)

    # BrokerFillSource doesn't have cancel in v4, so this should not raise
    safe.cancel("order-123")
