"""Dhan REST client mixin — PortfolioMixin.

Mixed into :class:`~tradex_brokers.dhan.client.DhanApiClient`; the
facade owns shared state and internal helpers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tradex_domain.enums import OrderSide, OrderType, ProductType
from tradex_domain.errors import AuthenticationError
from tradex_domain.execution import Account, PortfolioSnapshot, Position
from tradex_domain.instruments import Instrument
from tradex_domain.value_objects import AccountId, Money, Price

from tradex_brokers.common.provider_common import (
    as_decimal,
    first_mapping,
    is_token_rejection_response,
    unwrap_data,
)

if TYPE_CHECKING:
    from tradex_brokers.dhan._facade import DhanClientFacade


class PortfolioMixin:
    def get_account(self: DhanClientFacade) -> Account:
        """Account snapshot via GET /fundlimit."""
        body = self._request("GET", "/fundlimit", cache_read=True)
        # A token rejection (HTTP 401/DH-901 or 400/DH-906) would otherwise be
        # parsed into a misleading zero-balance Account — raise instead so
        # verify_connection() reports the truth.
        if isinstance(body, dict) and is_token_rejection_response(
            body.get("_http_status", 200), body
        ):
            raise AuthenticationError("Dhan rejected the access token on fundlimit")
        row = first_mapping(unwrap_data(body))
        amount = as_decimal(
            row.get("availabelBalance", row.get("availableBalance", row.get("sodLimit", 0)))
        )
        return Account(
            account_id=AccountId(value="dhan"),
            balance=Money(amount=amount, currency="INR"),
            margin=Money(amount=as_decimal(row.get("utilizedMargin", 0)), currency="INR"),
            equity=Money(amount=amount, currency="INR"))


    def profile(self: DhanClientFacade) -> dict[str, object]:
        """Account profile via GET /profile."""
        body = self._request("GET", "/profile", cache_read=True)
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def ledger(self: DhanClientFacade, from_date: str, to_date: str) -> list[dict[str, object]]:
        """Ledger entries via GET /ledgers."""
        params: dict[str, object] = {"fromDate": from_date, "toDate": to_date}
        body = self._request("GET", "/ledgers", cache_read=False, params=params)
        rows = unwrap_data(body)
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]


    def fund_limits(self: DhanClientFacade) -> dict[str, object]:
        """Raw fund-limit breakdown via GET /fundlimit."""
        body = self._request("GET", "/fundlimit", cache_read=True)
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def margin_calculator(
        self: DhanClientFacade,
        instrument: Instrument,
        *,
        side: OrderSide,
        quantity: int,
        product_type: ProductType = ProductType.INTRADAY,
        price: Price | None = None,
        order_type: OrderType | None = None) -> dict[str, object]:
        """Pre-trade margin via POST /margincalculator."""
        payload: dict[str, object] = {
            "dhanClientId": self._client_id,
            "exchangeSegment": self._segment(instrument),
            "transactionType": side.value,
            "quantity": int(quantity),
            "productType": self._native_product_type(product_type),
            "securityId": self._security_id(instrument.instrument_id),
            "price": float(price.value) if price is not None else 0.0,
        }
        if order_type is not None:
            if order_type is OrderType.STOP_LIMIT:
                payload["orderType"] = "STOP_LOSS"
            elif order_type is OrderType.STOP:
                payload["orderType"] = "STOP_LOSS" if price is not None else "STOP_LOSS_MARKET"
            else:
                payload["orderType"] = order_type.value
        body = self._request("POST", "/margincalculator", json=payload)
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def token_status(self: DhanClientFacade) -> dict[str, object]:
        """Token validity check via GET /tokenStatus."""
        body = self._request("GET", "/tokenStatus", cache_read=False)
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def get_positions(self: DhanClientFacade) -> list[Position]:
        """Open positions via GET /positions."""
        body = self._request("GET", "/positions", cache_read=True)
        return self._positions(unwrap_data(body))


    def get_holdings(self: DhanClientFacade) -> list[Position]:
        """Holdings via GET /holdings."""
        body = self._request("GET", "/holdings", cache_read=True)
        return self._positions(unwrap_data(body))


    def get_portfolio(self: DhanClientFacade) -> PortfolioSnapshot:
        """Portfolio snapshot (positions + account)."""
        return PortfolioSnapshot(
            positions=self.get_positions(), account=self._account_for_portfolio()
        )


    def convert_position(
        self: DhanClientFacade,
        instrument: Instrument,
        *,
        from_product: ProductType,
        to_product: ProductType,
        quantity: int,
        position_type: str = "LONG") -> dict[str, object]:
        """Convert position product type via POST /positions/convert."""
        payload = {
            "dhanClientId": self._client_id,
            "fromProductType": self._native_product_type(from_product),
            "exchangeSegment": self._segment(instrument),
            "positionType": position_type.upper(),
            "securityId": self._security_id(instrument.instrument_id),
            "tradingSymbol": instrument.symbol,
            "convertQty": int(quantity),
            "toProductType": self._native_product_type(to_product),
        }
        body = self._request(
            "POST", "/positions/convert", json=payload
        )
        self._invalidate_after_write()
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def exit_all(self: DhanClientFacade) -> dict[str, object]:
        """Close all positions and cancel all orders via POST /exitall."""
        body = self._request("POST", "/exitall", json={})
        self._invalidate_after_write()
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def mass_status(self: DhanClientFacade) -> dict[str, object]:
        """Whole-account snapshot: orders + positions + account."""
        return {
            "orders": self.get_orderbook(),
            "positions": self.get_positions(),
            "account": self.get_account(),
        }


    def get_trade_history(
        self: DhanClientFacade,
        instrument_id: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None) -> list[dict[str, object]]:
        """Trade history via GET /trades with optional filters."""
        params: dict[str, object] = {}
        if instrument_id:
            params["securityId"] = instrument_id
        if from_date:
            params["fromDate"] = from_date
        if to_date:
            params["toDate"] = to_date
        body = self._request(
            "GET", "/trades", cache_read=False, params=params or None
        )
        rows = unwrap_data(body)
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]


    def trade_book(self: DhanClientFacade) -> list[dict[str, object]]:
        """Today's executed trades via GET /trades (raw rows)."""
        body = self._request("GET", "/trades", cache_read=False)
        rows = unwrap_data(body)
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]

