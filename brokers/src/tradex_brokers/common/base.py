"""Shared broker adapter base.

``BaseBroker`` owns the cross-broker concerns that every adapter repeats today:
lifecycle (``connect``/``close``), the live-trading order gate, capability
checks, and the mechanical pass-throughs that simply delegate to the composed
API client.  Per-broker adapters subclass this and implement only what is
genuinely specific (native option-chain fallbacks, super/forever/slice/edis,
streaming backends, instrument loading).

This is a behavioral extraction — the public ``BrokerAdapter`` surface and
signatures are unchanged, so ``BrokerFactory.register``'s
``isinstance(adapter, BrokerAdapter)`` check keeps passing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from tradex_domain.capabilities import BrokerCapabilities, require_capability
from tradex_domain.errors import BrokerUnavailableError, OrderRejectedError
from tradex_domain.execution import (
    Account,
    Order,
    OrderRequest,
    OrderResult,
    PortfolioSnapshot,
    Position,
)
from tradex_domain.instruments import Equity, Index, Instrument
from tradex_domain.market import Depth, HistoricalSeries, Quote, require_depth_supported
from tradex_domain.value_objects import InstrumentId, OrderId, Price
from tradex_domain.wire import InstrumentRegistry

from tradex_brokers.common.provider_common import (
    build_instrument_from_row,
    future_chain_from_master,
)
from tradex_brokers.common.token_lifecycle import TokenLifecyclePort

log = logging.getLogger(__name__)


class BaseBroker:
    """Common lifecycle, gating, and pass-through delegation for adapters."""

    #: Subclasses assign the composed API client here (``DhanApiClient``,
    #: ``UpstoxApiClient`` …). Pass-throughs delegate to it.
    _transport: Any

    #: Fallback universe used when no master rows are loaded (``search`` /
    #: ``_universe``). Subclasses override with their broker constants.
    _fallback_equities: tuple[str, ...] = ()
    _fallback_index_keys: dict[str, str] = {}

    def __init__(
        self,
        *,
        capabilities: BrokerCapabilities,
        transport: Any = None,
        registry: Any = None,
        allow_order_operations: bool = True,
        instrument_loader: Callable[[], Iterable[Mapping[str, Any]]] | None = None,
    ) -> None:
        self._transport = transport
        self._registry = registry or InstrumentRegistry()
        self._loaded_instruments: list[Instrument] = []
        self._connected = False
        self._allow_order_operations = allow_order_operations
        self._capabilities = capabilities
        self._instrument_loader = instrument_loader
        self._instruments_loaded = False
        self._token_manager: TokenLifecyclePort | None = None
        self.master_loader: Any | None = None
        self._ws_backend: Any | None = None
        self._order_backend: Any | None = None
        self._depth_backend: Any | None = None

    # ------------------------------------------------------------------
    # capabilities
    # ------------------------------------------------------------------

    @property
    def capabilities(self) -> BrokerCapabilities:
        return self._capabilities

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Establish connection; load instruments from the loader if present."""
        if self._transport is None:
            # No transport — stay capability-loud but logically connected.
            self._connected = True
            return
        if self._instrument_loader is not None and not self._instruments_loaded:
            self.load_instruments(self._instrument_loader())
            self._instruments_loaded = True
        self._connected = True

    def close(self) -> None:
        """Tear down connection — closes every owned stream backend."""
        self._connected = False
        self._teardown_stream_backends()

    def _teardown_stream_backends(self) -> None:
        """Default teardown; subclasses extend for extra backends."""
        for attr in ("_ws_backend", "_order_backend", "_depth_backend"):
            backend = getattr(self, attr, None)
            if backend is not None:
                backend.close()
                setattr(self, attr, None)

    def verify_connection(self) -> bool:
        """Non-destructive health check: local state first, cheap probe second.

        Returns True only when the broker is connected AND the transport
        can reach the provider account endpoint.  Does NOT invalidate the
        read cache — verification must not mutate state.
        """
        if self._transport is None or not self._connected:
            return False
        try:
            self._transport.get_account()
            return True
        except Exception:
            return False

    def ensure_master_fresh(self, *, force_refresh: bool = False) -> None:
        """Re-run the instrument-master loader (daily refresh hook)."""
        loader = self.master_loader
        if loader is None:
            return
        self.load_instruments(loader.load(force_refresh=force_refresh))

    # ------------------------------------------------------------------
    # gating / requirement helpers
    # ------------------------------------------------------------------

    def _require(self) -> Any:
        if self._transport is None or not self._connected:
            raise BrokerUnavailableError(
                f"{type(self).__name__} not connected"
            )
        return self._transport

    def _require_mutation(self) -> None:
        if not self._allow_order_operations:
            raise OrderRejectedError("live order gate disabled")

    def set_order_operations_enabled(self, enabled: bool) -> None:
        self._allow_order_operations = enabled

    def load_instruments(
        self, rows: Iterable[Mapping[str, Any]] | None = None
    ) -> None:
        """Load decoded master rows, or the deterministic fallback universe.

        Master loads build a fresh registry and swap it in atomically
        (``replace_all``): concurrent WS-tick resolution against the shared
        registry sees either the old or the new complete master, never a
        partial reload. Subclasses override :meth:`_extra_row_meta` and
        :meth:`_extra_row_aliases` for broker-specific metadata/aliases.
        """
        if rows is None:
            self._load_fallback_universe()
            return
        fresh = InstrumentRegistry()
        loaded: list[Instrument] = []
        for row in rows:
            exchange = str(row.get("exchange", "NSE")).strip().upper()
            key = str(row.get("key", f"{exchange}:{row.get('symbol', '')}"))
            asset_class = str(row.get("asset_class", "EQUITY")).upper()
            instrument = build_instrument_from_row(row)
            iid = instrument.instrument_id
            # Authoritative registration: a full-master load owns the
            # primary provider key, so a re-listed/rotated security id in a
            # fresh download re-points on refresh. Bare ids registered by
            # REST chain endpoints stay aliases (never authoritative).
            meta: dict[str, object] = {"asset_class": asset_class}
            native_kind = str(row.get("instrument_type") or "").strip()
            if native_kind:
                meta["instrument_type"] = native_kind
            self._extra_row_meta(row, meta)
            fresh.register_authoritative(iid, key, meta)
            fresh.add_alias(key, iid)
            fresh.add_alias(str(row.get("symbol", "")).strip().upper(), iid)
            self._extra_row_aliases(fresh, row, iid, key)
            loaded.append(instrument)
        # Publish the new instrument list before the atomic registry swap so
        # search/_universe never see a new registry with an old list.
        self._loaded_instruments = loaded
        # Master CSVs never carry index instruments (NIFTY, BANKNIFTY, …).
        # Register the fallback index keys so option-chain / ticker calls
        # always resolve them — even after a full master load.
        for symbol, native_key in self._fallback_index_keys.items():
            index = Index.of("IDX", symbol)
            iid = index.instrument_id
            fresh.register(iid, {"key": native_key, "asset_class": "INDEX"})
            fresh.add_alias(native_key, iid)
            fresh.add_alias(symbol, iid)
        self._registry.replace_all(fresh)
        self._instruments_loaded = True

    def _extra_row_meta(self, row: Mapping[str, Any], meta: dict[str, object]) -> None:
        """Hook: broker-specific contract metadata (lot/tick sizes)."""

    def _extra_row_aliases(
        self,
        fresh: InstrumentRegistry,
        row: Mapping[str, Any],
        iid: InstrumentId,
        key: str,
    ) -> None:
        """Hook: broker-specific alias registration (e.g. bare security ids)."""

    def _load_fallback_universe(self) -> None:
        """Register the deterministic fallback universe into the registry."""
        for symbol in self._fallback_equities:
            iid = InstrumentId.equity("NSE", symbol)
            self._registry.register(iid, {"key": f"NSE:{symbol}", "asset_class": "EQUITY"})
            self._registry.add_alias(symbol, iid)
        for symbol, native_key in self._fallback_index_keys.items():
            index = Index.of("IDX", symbol)
            iid = index.instrument_id
            self._registry.register(iid, {"key": native_key, "asset_class": "INDEX"})
            self._registry.add_alias(native_key, iid)
            self._registry.add_alias(symbol, iid)

    # ------------------------------------------------------------------
    # orders — common pass-throughs
    # ------------------------------------------------------------------

    def submit_order(self, request: OrderRequest) -> OrderId:
        self._require_mutation()
        return self._require().submit_order(request)

    def cancel_order(self, order_id: OrderId) -> Order:
        self._require_mutation()
        return self._require().cancel_order(order_id)

    def modify_order(self, order_id: OrderId, request: OrderRequest) -> Order:
        self._require_mutation()
        return self._require().modify_order(order_id, request)

    def get_order(self, order_id: OrderId) -> Order:
        return self._require().get_order(order_id)

    def get_orderbook(self) -> list[Order]:
        return list(self._require().get_orderbook())

    def get_order_by_correlation_id(self, tag: str) -> dict[str, object]:
        return self._require().get_order_by_correlation_id(tag)

    # ------------------------------------------------------------------
    # portfolio — common pass-throughs
    # ------------------------------------------------------------------

    def get_positions(self) -> list[Position]:
        return list(self._require().get_positions())

    def get_holdings(self) -> list[Position]:
        return list(self._require().get_holdings())

    def get_account(self) -> Account:
        return self._require().get_account()

    def get_portfolio(self) -> PortfolioSnapshot:
        return self._require().get_portfolio()

    # ------------------------------------------------------------------
    # market data — common pass-throughs
    # ------------------------------------------------------------------

    def get_quote(self, instrument: Instrument) -> Quote:
        return self._require().get_quote(instrument)

    def ltp(self, instrument: Instrument) -> Price:
        return self._require().ltp(instrument)

    def depth(self, instrument: Instrument) -> Depth:
        require_depth_supported(instrument)
        return self._require().depth(instrument)

    def history(
        self,
        instrument: Instrument,
        timeframe: object = None,
        start: datetime | None = None,
        end: datetime | None = None,
        *,
        interval: str | None = None,
        lookback_days: int | None = None,
    ) -> HistoricalSeries:
        """Get historical data.
        
        Canonical: history(inst, timeframe, start, end).
        Convenience: history(inst, interval="5m", lookback_days=5).
        """
        from tradex_domain.enums import Timeframe
        
        if interval is not None:
            timeframe = Timeframe(interval)
        if timeframe is None:
            raise ValueError("history requires a timeframe or interval")
        if not isinstance(timeframe, Timeframe):
            timeframe = Timeframe(timeframe)
        if start is None or end is None:
            if lookback_days is None:
                lookback_days = 30
            from datetime import UTC
            end = end or datetime.now(UTC)
            start = start or (end - timedelta(days=lookback_days))
        return self._require().history(instrument, timeframe, start, end)

    def search(self, query: str) -> list[Instrument]:
        q = query.strip().upper()
        return [i for i in self._universe() if q in i.symbol.upper()]

    def _universe(self) -> list[Instrument]:
        fallback: list[Instrument] = [
            Equity.of("NSE", s) for s in self._fallback_equities
        ]
        fallback.extend(Index.of("IDX", s) for s in self._fallback_index_keys)
        by_id: dict[str, Instrument] = {
            str(item.instrument_id): item for item in fallback
        }
        by_id.update(
            {str(item.instrument_id): item for item in self._loaded_instruments}
        )
        return list(by_id.values())

    # ------------------------------------------------------------------
    # market data — capability-gated batch / chain helpers
    # ------------------------------------------------------------------

    def ltp_batch(self, instruments: Iterable[Instrument]) -> dict[InstrumentId, Price]:
        require_capability(self._capabilities, "supports_batch_market_data")
        return self._require().ltp_batch(list(instruments))

    def quote_batch(self, instruments: Iterable[Instrument]) -> dict[InstrumentId, Quote]:
        require_capability(self._capabilities, "supports_batch_market_data")
        return self._require().quote_batch(list(instruments))

    def future_chain(self, underlying: Instrument) -> list[Instrument]:
        """Futures on *underlying* from the loaded master (no broker endpoint)."""
        require_capability(self._capabilities, "supports_future_chain")
        return future_chain_from_master(self._loaded_instruments, underlying)

    # ------------------------------------------------------------------
    # ExtensionAdapter — capability-gated pass-throughs shared by all
    # adapters that support the flags (super / forever / slice / eDIS,
    # kill switch, auxiliary account surface).
    # ------------------------------------------------------------------

    def submit_super_order(self, request: OrderRequest) -> OrderId:
        self._require_mutation()
        require_capability(self._capabilities, "supports_super_order")
        return self._require().submit_super_order(request)

    def modify_super_order(
        self, order_id: OrderId, request: OrderRequest
    ) -> OrderResult:
        self._require_mutation()
        require_capability(self._capabilities, "supports_super_order")
        return self._require().modify_super_order(order_id, request)

    def cancel_super_order(
        self, order_id: OrderId, leg: str = "ENTRY"
    ) -> OrderResult:
        self._require_mutation()
        require_capability(self._capabilities, "supports_super_order")
        return self._require().cancel_super_order(order_id, leg)

    def list_super_orders(self) -> list[OrderResult]:
        require_capability(self._capabilities, "supports_super_order")
        return list(self._require().list_super_orders())

    def submit_forever_order(self, request: OrderRequest) -> OrderId:
        self._require_mutation()
        require_capability(self._capabilities, "supports_forever_order")
        return self._require().submit_forever_order(request)

    def submit_slice_order(
        self,
        request: OrderRequest,
        slices: int,
        interval: timedelta | None = None,
    ) -> list[OrderId]:
        self._require_mutation()
        require_capability(self._capabilities, "supports_slice_order")
        return list(self._require().submit_slice_order(request, slices, interval))

    def submit_edis(self, request: OrderRequest) -> OrderId:
        self._require_mutation()
        require_capability(self._capabilities, "supports_edis")
        return self._require().submit_edis(request)

    def modify_forever_order(
        self, order_id: OrderId, request: OrderRequest
    ) -> OrderResult:
        self._require_mutation()
        require_capability(self._capabilities, "supports_forever_order")
        return self._require().modify_forever_order(order_id, request)

    def cancel_forever_order(self, order_id: OrderId) -> OrderResult:
        self._require_mutation()
        require_capability(self._capabilities, "supports_forever_order")
        return self._require().cancel_forever_order(order_id)

    def list_forever_orders(self) -> list[OrderResult]:
        require_capability(self._capabilities, "supports_forever_order")
        return list(self._require().list_forever_orders())

    def kill_switch(self, enable: bool = True) -> dict[str, object]:
        self._require_mutation()
        require_capability(self._capabilities, "supports_kill_switch")
        return self._require().kill_switch(enable)

    def status_kill_switch(self) -> dict[str, object]:
        require_capability(self._capabilities, "supports_kill_switch")
        return self._require().status_kill_switch()

    def exit_all(self) -> dict[str, object]:
        self._require_mutation()
        return self._require().exit_all()

    def profile(self) -> dict[str, object]:
        return self._require().profile()

    def ledger(self, from_date: str, to_date: str) -> list[dict[str, object]]:
        return list(self._require().ledger(from_date, to_date))

    def fund_limits(self) -> dict[str, object]:
        return self._require().fund_limits()

    def token_status(self) -> dict[str, object]:
        return self._require().token_status()

    @property
    def registry(self) -> Any:
        return self._registry


__all__ = ["BaseBroker"]
