"""Upstox REST client mixin - AdminMixin. """

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tradex_brokers.upstox._facade import UptoxFacade


from tradex_brokers.common.provider_common import unwrap_data


class AdminMixin:
    def kill_switch(self: UptoxFacade, enable: bool = True) -> dict[str, object]:
        """Broker-side kill switch via PUT /user/kill-switch."""
        status = "ENABLED" if enable else "DISABLED"
        body = self._request(
            "PUT", "/user/kill-switch", json={"kill_switch_status": [{"status": status}]})
        self._invalidate_after_write()
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}

    def status_kill_switch(self: UptoxFacade) -> dict[str, object]:
        """Kill-switch state via GET /user/kill-switch."""
        body = self._request(
            "GET", "/user/kill-switch", cache_read=False
        )
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}

    def invalidate_read_cache(self: UptoxFacade) -> None:
        """Drop cached read responses so a verification probe hits the wire."""
        self._http.invalidate_cache()

