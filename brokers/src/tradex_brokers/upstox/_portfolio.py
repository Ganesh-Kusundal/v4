"""Upstox REST client mixin - PortfolioMixin. """

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from tradex_domain.enums import OrderSide, ProductType
from tradex_domain.errors import AuthenticationError
from tradex_domain.execution import Account, PortfolioSnapshot, Position
from tradex_domain.instruments import Instrument
from tradex_domain.value_objects import AccountId, Money, Price

if TYPE_CHECKING:
    from tradex_brokers.upstox._facade import UptoxFacade

from tradex_brokers.common.provider_common import (
    as_decimal,
    first_mapping,
    is_token_rejection_response,
    provider_key,
    unwrap_data,
)


class PortfolioMixin:
    def get_account(self: UptoxFacade) -> Account:
        """Account snapshot via GET /user/get-funds-and-margin (v3 host)."""
        body = self._request(
            "GET", "/user/get-funds-and-margin", host="v3", cache_read=True
        )
        # A token rejection would otherwise be parsed into a misleading
        # zero-balance Account — raise instead so verify_connection() is honest.
        if isinstance(body, dict) and is_token_rejection_response(
            body.get("_http_status", 200), body
        ):
            raise AuthenticationError("Upstox rejected the access token on funds")
        row = first_mapping(unwrap_data(body))
        available = row.get("available_to_trade")
        if isinstance(available, dict):
            amount = as_decimal(
                str(available.get("total", available.get("cash_available_to_trade")))
            )
        else:
            equity = row.get("equity", row)
            equity_row = equity if isinstance(equity, dict) else {}
            amount = as_decimal(
                str(equity_row.get("available_margin", equity_row.get("available_balance")))
            )
        return Account(
            account_id=AccountId(value="upstox"),
            balance=Money(amount=amount, currency="INR"),
            margin=Money(amount=Decimal(0), currency="INR"),
            equity=Money(amount=amount, currency="INR"))

    def profile(self: UptoxFacade) -> dict[str, object]:
        """Account profile via GET /user/profile."""
        body = self._request("GET", "/user/profile", cache_read=True)
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}

    def token_status(self: UptoxFacade) -> dict[str, object]:
        """Token validity check (delegates to profile endpoint)."""
        return self.profile()

    def ledger(self: UptoxFacade, from_date: str, to_date: str) -> list[dict[str, object]]:
        """Fund ledger via GET /reports/funds/ledger."""
        params: dict[str, object] = {"from_date": from_date, "to_date": to_date}
        body = self._request(
            "GET", "/reports/funds/ledger", cache_read=False, params=params
        )
        rows = unwrap_data(body)
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
        if isinstance(rows, dict):
            items = rows.get("ledgers", rows.get("data", []))
            return [r for r in items if isinstance(r, dict)] if isinstance(items, list) else []
        return []

    def fund_limits(self: UptoxFacade) -> dict[str, object]:
        """Raw funds-and-margin breakdown via GET /user/get-funds-and-margin (v3 host)."""
        body = self._request(
            "GET", "/user/get-funds-and-margin", host="v3", cache_read=True)
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}

    def margin(
        self: UptoxFacade,
        instrument: Instrument,
        *,
        side: OrderSide,
        quantity: int,
        product_type: ProductType = ProductType.INTRADAY,
        price: Price | None = None) -> dict[str, object]:
        """Pre-trade margin via POST /margin/requirement."""
        payload = {
            "instrument_token": provider_key(self._registry, instrument.instrument_id),
            "transaction_type": side.value,
            "quantity": int(quantity),
            "price": float(price.value) if price is not None else 0.0,
            "product": self._native_product_type(product_type),
        }
        body = self._request(
            "POST", "/margin/requirement", json=payload
        )
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}

    def convert_position(
        self: UptoxFacade,
        instrument: Instrument,
        *,
        from_product: ProductType,
        to_product: ProductType,
        quantity: int) -> dict[str, object]:
        """Convert a position's product type via PUT /portfolio/convert-position."""
        payload = {
            "instrument_token": provider_key(self._registry, instrument.instrument_id),
            "from_product": self._native_product_type(from_product),
            "to_product": self._native_product_type(to_product),
            "quantity": int(quantity),
        }
        body = self._request(
            "PUT", "/portfolio/convert-position", json=payload
        )
        self._invalidate_after_write()
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}

    def get_positions(self: UptoxFacade) -> list[Position]:
        """Open positions via GET /portfolio/short-term-positions."""
        body = self._request(
            "GET", "/portfolio/short-term-positions", cache_read=True
        )
        return self._positions(unwrap_data(body))

    def get_holdings(self: UptoxFacade) -> list[Position]:
        """Holdings via GET /portfolio/long-term-holdings."""
        body = self._request(
            "GET", "/portfolio/long-term-holdings", cache_read=True
        )
        return self._positions(unwrap_data(body))

    def get_portfolio(self: UptoxFacade) -> PortfolioSnapshot:
        """Portfolio snapshot (positions + account)."""
        return PortfolioSnapshot(
            positions=self.get_positions(), account=self._account_for_portfolio()
        )

    def get_trade_book(self: UptoxFacade) -> list[dict[str, object]]:
        """Today's executed trades via GET /order/trades/get-trades-for-day."""
        body = self._request(
            "GET", "/order/trades/get-trades-for-day", cache_read=False
        )
        rows = unwrap_data(body)
        if not isinstance(rows, list):
            return []
        return [r for r in rows if isinstance(r, dict)]

    def get_trade_pnl(
        self: UptoxFacade,
        segment: str,
        financial_year: str,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        page_number: int = 1,
        page_size: int = 100) -> dict[str, object]:
        """Trade-wise realised P&L via GET /trade/profit-loss/data."""
        params: dict[str, object] = {
            "segment": segment,
            "financial_year": financial_year,
            "page_number": page_number,
            "page_size": page_size,
        }
        if from_date:
            params["from_date"] = from_date
        if to_date:
            params["to_date"] = to_date
        body = self._request(
            "GET", "/trade/profit-loss/data", cache_read=False, params=params
        )
        return body if isinstance(body, dict) else {}

    def exit_all(self: UptoxFacade) -> dict[str, object]:
        """Exit all open positions via POST /order/positions/exit."""
        body = self._request("POST", "/order/positions/exit")
        self._invalidate_after_write()
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}

