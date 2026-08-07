"""Dhan REST client mixin — AdminMixin.

Mixed into :class:`~tradex_brokers.dhan.client.DhanApiClient`; the
facade owns shared state and internal helpers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tradex_domain.execution import OrderRequest
from tradex_domain.value_objects import OrderId

from tradex_brokers.common.provider_common import unwrap_data

if TYPE_CHECKING:
    from tradex_brokers.dhan._facade import DhanClientFacade


class AdminMixin:
    def submit_edis(self: DhanClientFacade, request: OrderRequest) -> OrderId:
        """Authorize eDIS via POST /edis/authorize."""
        isin = request.instrument.meta.isin or request.instrument.symbol
        body = self._request(
            "POST", "/edis/authorize", json={
                "isin": isin,
                "quantity": int(request.quantity.value),
                "exchange": request.instrument.exchange.value,
            })
        self._invalidate_after_write()
        return self._response_order_id(body, "Dhan eDIS")


    def authorize_edis(
        self: DhanClientFacade,
        isin: str,
        quantity: int,
        exchange: str,
    ) -> dict[str, object]:
        """eDIS authorization via POST /edis/authorize."""
        body = self._request(
            "POST",
            "/edis/authorize",
            json={"isin": isin, "quantity": quantity, "exchange": exchange})
        self._invalidate_after_write()
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def edis_status(self: DhanClientFacade, isin: str) -> dict[str, object]:
        """eDIS authorization status via GET /edis/status/{isin}."""
        body = self._request("GET", f"/edis/status/{isin}", cache_read=False)
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def kill_switch(self: DhanClientFacade, enable: bool = True) -> dict[str, object]:
        """Broker-side kill switch via POST /killswitch."""
        action = "ACTIVATE" if enable else "DEACTIVATE"
        body = self._request(
            "POST", f"/killswitch?killSwitchStatus={action}", json={}
        )
        self._invalidate_after_write()
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def status_kill_switch(self: DhanClientFacade) -> dict[str, object]:
        """Kill-switch state via GET /killswitch."""
        body = self._request("GET", "/killswitch", cache_read=False)
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def configure_pnl_exit(self: DhanClientFacade, **params: object) -> dict[str, object]:
        """Configure PnL exit via POST /pnlExit."""
        body = self._request("POST", "/pnlExit", json=params)
        self._invalidate_after_write()
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def stop_pnl_exit(self: DhanClientFacade) -> dict[str, object]:
        """Stop PnL exit via DELETE /pnlExit."""
        body = self._request("DELETE", "/pnlExit")
        self._invalidate_after_write()
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def get_pnl_exit(self: DhanClientFacade) -> dict[str, object]:
        """Get PnL exit config via GET /pnlExit."""
        body = self._request("GET", "/pnlExit", cache_read=False)
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def set_ip(self: DhanClientFacade, **params: object) -> dict[str, object]:
        """Set IP configuration via POST /ip/setIP."""
        body = self._request("POST", "/ip/setIP", json=params)
        self._invalidate_after_write()
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def modify_ip(self: DhanClientFacade, **params: object) -> dict[str, object]:
        """Modify IP configuration via PUT /ip/modifyIP."""
        body = self._request("PUT", "/ip/modifyIP", json=params)
        self._invalidate_after_write()
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def get_ip(self: DhanClientFacade) -> dict[str, object]:
        """Get IP configuration via GET /ip/getIP."""
        body = self._request("GET", "/ip/getIP", cache_read=False)
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def invalidate_read_cache(self: DhanClientFacade) -> None:
        """Drop cached read responses so a verification probe hits the wire."""
        self._http.invalidate_cache()

