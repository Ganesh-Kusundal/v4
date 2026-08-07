"""Dhan REST client mixin — AlertsMixin.

Mixed into :class:`~tradex_brokers.dhan.client.DhanApiClient`; the
facade owns shared state and internal helpers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tradex_brokers.common.provider_common import unwrap_data

if TYPE_CHECKING:
    from tradex_brokers.dhan._facade import DhanClientFacade


class AlertsMixin:
    def place_alert(self: DhanClientFacade, **params: object) -> dict[str, object]:
        """Place price alert via POST /alerts."""
        body = self._request("POST", "/alerts", json=params)
        self._invalidate_after_write()
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def get_alert(self: DhanClientFacade, alert_id: str) -> dict[str, object]:
        """Get alert via GET /alerts/{id}."""
        body = self._request("GET", f"/alerts/{alert_id}", cache_read=False)
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def list_alerts(self: DhanClientFacade) -> list[dict[str, object]]:
        """List all alerts via GET /alerts."""
        body = self._request("GET", "/alerts", cache_read=False)
        rows = unwrap_data(body)
        if not isinstance(rows, list):
            return []
        return [r for r in rows if isinstance(r, dict)]


    def delete_alert(self: DhanClientFacade, alert_id: str) -> dict[str, object]:
        """Delete alert via DELETE /alerts/{id}."""
        body = self._request("DELETE", f"/alerts/{alert_id}")
        self._invalidate_after_write()
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def place_conditional_trigger(self: DhanClientFacade, **params: object) -> dict[str, object]:
        """Place conditional trigger via POST /alerts/orders."""
        body = self._request("POST", "/alerts/orders", json=params)
        self._invalidate_after_write()
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def get_conditional_trigger(self: DhanClientFacade, alert_id: str) -> dict[str, object]:
        """Get conditional trigger via GET /alerts/orders/{id}."""
        body = self._request(
            "GET", f"/alerts/orders/{alert_id}", cache_read=False
        )
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def list_conditional_triggers(self: DhanClientFacade) -> list[dict[str, object]]:
        """List all conditional triggers via GET /alerts/orders."""
        body = self._request(
            "GET", "/alerts/orders", cache_read=False
        )
        rows = unwrap_data(body)
        if not isinstance(rows, list):
            return []
        return [r for r in rows if isinstance(r, dict)]


    def delete_conditional_trigger(self: DhanClientFacade, alert_id: str) -> dict[str, object]:
        """Delete conditional trigger via DELETE /alerts/orders/{id}."""
        body = self._request(
            "DELETE", f"/alerts/orders/{alert_id}")
        self._invalidate_after_write()
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def modify_conditional_trigger(
        self: DhanClientFacade,
        alert_id: str,
        **params: object,
    ) -> dict[str, object]:
        """Modify conditional trigger via PUT /alerts/orders/{id}."""
        body = self._request(
            "PUT", f"/alerts/orders/{alert_id}", json=params
        )
        self._invalidate_after_write()
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}

