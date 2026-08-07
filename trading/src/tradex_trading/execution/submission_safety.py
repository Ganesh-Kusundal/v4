"""Safe wrapper for BrokerFillSource with OrderSubmissionUnknownError handling."""

from __future__ import annotations

from tradex_domain.errors import OrderSubmissionUnknownError

from tradex_trading.execution.fill_sources import BrokerFillSource


class SafeBrokerFillSource:
    """Wraps BrokerFillSource with OrderSubmissionUnknownError handling."""

    def __init__(self, broker: object):
        self._inner = BrokerFillSource(broker)

    @property
    def submission_boundary_crossed(self) -> bool:
        return self._inner.submission_boundary_crossed

    def submit(self, request):
        try:
            return self._inner.submit(request)
        except (OSError, TimeoutError, ConnectionError) as exc:
            if self._inner.submission_boundary_crossed:
                raise OrderSubmissionUnknownError(
                    f"Order submission failed after crossing broker boundary: {exc}"
                ) from exc
            raise

    def cancel(self, order_id):
        if hasattr(self._inner, 'cancel'):
            self._inner.cancel(order_id)
