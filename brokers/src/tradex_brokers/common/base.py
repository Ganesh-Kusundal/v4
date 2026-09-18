"""Shared broker adapter base.

``BaseBroker`` owns the cross-broker concerns that every adapter repeats today:
lifecycle (``connect``/``close``), the live-trading order gate, capability
checks, and the mechanical pass-throughs that simply delegate to the composed
API client.  Per-broker adapters subclass this and implement only what is
genuinely specific (native option-chain fallbacks, super/forever/slice/edis,
streaming backends, instrument loading).

Design note — the pass-through wall (G4)
-----------------------------------------
The mechanical one-line delegations to ``self._transport`` are generated
from a single declarative spec (:attr:`_PASSTHROUGH_WALL` + the
``_install_passthrough_wall`` installer). Each generated method keeps the
same body shape the hand-written version had — lifecycle gate
(``_require``), the mutation gate (``_require_mutation``), and/or a
capability check (``require_capability``) — so the per-method policy stays
visible in one place, every method has a normal entry in the class body
(per-method stack traces, IDE navigation), and adding a new standard
broker operation is a one-line edit to the spec rather than a new method
definition. A broker-specific override (custom risk policy, etc.) stays a
regular method and is preserved by the installer.

Design note — two order gates, on purpose
-----------------------------------------
``_allow_order_operations`` (adapter level) is a hardware-style safety: an
adapter constructed with ``allow_order_operations=False`` cannot trade even if
mis-wired into a permissive session. The session's ``order_gate`` (service
level) is policy: it flips with ``live_orders_enabled`` at runtime. They guard
different failure modes; merging them removes a layer of protection.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from tradex_domain.capabilities import BrokerCapabilities, require_capability
from tradex_domain.errors import BrokerUnavailableError, OrderRejectedError
from tradex_domain.instruments import Equity, Index, Instrument
from tradex_domain.market import Depth, HistoricalSeries, Quote, require_depth_supported
from tradex_domain.protocols import (
    DepthStreamPort,
    MarketStreamPort,
    OrderStreamPort,
)
from tradex_domain.value_objects import InstrumentId, Price
from tradex_domain.wire import InstrumentRegistry

from tradex_brokers.common.provider_common import (
    build_instrument_from_row,
    future_chain_from_master,
)
from tradex_brokers.common.token_lifecycle import TokenLifecyclePort

log = logging.getLogger(__name__)


def _install_passthrough_wall(cls: type[BaseBroker]) -> None:
    """Generate the standard pass-through methods from :attr:`_PASSTHROUGH_WALL`.

    See :class:`BaseBroker` for the spec format and the equivalent hand-written
    bodies. Existing attributes on ``cls`` are not overwritten — adapters that
    override a method (e.g. with broker-specific policy) keep their override.
    """
    for name, gate, arg_names, wraps_list, defaults in cls._PASSTHROUGH_WALL:
        if name in cls.__dict__:
            # An explicit override on the class (e.g. custom risk policy).
            continue
        # Split args into required + defaulted, since Python requires every
        # positional after the first default to also have a default.
        defaulted = {n: defaults[n] for n in arg_names if n in defaults}
        required = [n for n in arg_names if n not in defaults]
        param_parts: list[str] = []
        param_parts.extend(required)
        param_parts.extend(f"{n}={defaulted[n]}" for n in defaulted)
        params = ", ".join(param_parts)
        call_args = ", ".join(arg_names)
        body_lines: list[str] = []
        if gate == "read":
            pass
        elif gate == "mutation":
            body_lines.append("    self._require_mutation()")
        elif gate.startswith("mutation+cap:"):
            body_lines.append("    self._require_mutation()")
            cap = gate.split(":", 1)[1]
            body_lines.append(
                f'    require_capability(self._capabilities, "{cap}")'
            )
        elif gate.startswith("cap:"):
            cap = gate.split(":", 1)[1]
            body_lines.append(
                f'    require_capability(self._capabilities, "{cap}")'
            )
        else:  # pragma: no cover — guarded by the literal declaration
            raise AssertionError(f"unknown gate policy: {gate!r}")
        if wraps_list:
            body_lines.append(
                f"    return list(self._require().{name}({call_args}))"
            )
        else:
            body_lines.append(
                f"    return self._require().{name}({call_args})"
            )
        method_src = (
            f"def {name}(self{', ' + params if params else ''}):\n"
            + "\n".join(body_lines)
            + "\n"
        )
        # ``exec`` the method body with the module globals so that
        # ``require_capability`` (imported at the top of this module) is
        # resolvable. The local ``ns`` collects the freshly defined function.
        ns: dict[str, Any] = {}
        exec(method_src, globals(), ns)  # noqa: S102 — trusted local source
        setattr(cls, name, ns[name])


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
        self._ws_backend: MarketStreamPort | None = None
        self._order_backend: OrderStreamPort | None = None
        self._depth_backend: DepthStreamPort | None = None
        #: Guards lazy stream-backend creation/teardown so concurrent first
        #: subscribes cannot create duplicate WebSocket backends.
        self._stream_lock = threading.Lock()

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
        with self._stream_lock:
            for attr in ("_ws_backend", "_order_backend", "_depth_backend"):
                backend = getattr(self, attr, None)
                if backend is not None:
                    backend.close()
                    setattr(self, attr, None)

    def health(self) -> dict[str, bool]:
        """Coarse lifecycle/credential status — zero network traffic.

        Distinguishes the states ``verify_connection`` collapses:

        - ``configured``: a transport and/or token manager is bound.
        - ``connected``: :meth:`connect` has completed (transport usable).
        - ``authenticated``: the token manager holds a locally-unexpired
          token (absent token manager ⇒ assumed authenticated).
        - ``ready``: all of the above.

        Use this for schedulers/dashboards; use :meth:`verify_connection`
        for the authoritative wire-level probe.
        """
        configured = self._transport is not None or self._token_manager is not None
        authenticated = True
        is_expired = getattr(self._token_manager, "is_expired", None)
        if callable(is_expired):
            try:
                authenticated = not is_expired()
            except Exception:  # noqa: BLE001 — broken local clock ⇒ assume stale
                authenticated = False
        return {
            "configured": configured,
            "connected": self._connected,
            "authenticated": authenticated,
            "ready": configured and self._connected and authenticated,
        }

    def verify_connection(self) -> bool:
        """Non-destructive health check: local state first, cheap probe second.

        Returns True only when the broker is connected AND the transport
        can reach the provider account endpoint.  Does NOT invalidate the
        read cache — verification must not mutate state.

        A locally-expired token is NOT by itself a failure. Mint/refresh-
        capable managers (TOTP/refresh ``MintTokenManager``) can produce a
        fresh token on demand, so verification obtains one and then runs the
        wire probe — otherwise the probe would report False for a connection
        that every real request would authenticate. The probe is skipped only
        when the manager reports the token expired AND cannot obtain a
        replacement (no ``ensure_token``, or the on-demand mint/refresh
        fails): no point spending provider quota to learn what the manager
        already knows.
        """
        if self._transport is None or not self._connected:
            return False
        expired = False
        is_expired = getattr(self._token_manager, "is_expired", None)
        if callable(is_expired):
            try:
                expired = bool(is_expired())
            except Exception:  # noqa: BLE001 — broken local clock ⇒ assume stale; probe anyway
                expired = False
        if expired:
            ensure = getattr(self._token_manager, "ensure_token", None)
            if not callable(ensure):
                return False
            try:
                ensure()
            except Exception:  # noqa: BLE001 — mint/refresh failed ⇒ cannot verify
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
    # declared binding seams [REF-5] — replaces cross-object private writes
    # ------------------------------------------------------------------

    def set_token_manager(self, token_manager: TokenLifecyclePort | None) -> None:
        """Bind a token lifecycle manager post-construction.

        Public seam used by ``DhanBroker.from_fetch`` / ``UpstoxBroker.from_fetch``.
        """
        self._token_manager = token_manager

    def bind_stream_backend(self, kind: str, backend: Any) -> None:
        """Attach a stream backend: ``kind`` ∈ {"market", "order", "depth"}.

        Public seam used by ``runtime/live.py``; replaces the former
        ``broker._ws_backend = ...`` private-attribute write.
        """
        attr = {
            "market": "_ws_backend",
            "order": "_order_backend",
            "depth": "_depth_backend",
        }.get(kind)
        if attr is None:
            raise ValueError(f"unknown stream backend kind: {kind!r}")
        setattr(self, attr, backend)

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
        partial reload. Subclasses override :meth:`_extra_row_meta`,
        :meth:`_extra_row_aliases` and :meth:`_row_claim_priority` for
        broker-specific metadata, aliases and symbol-claim strength.
        """
        if rows is None:
            self._load_fallback_universe()
            return
        # Registration order decides who owns the primary provider key, because
        # ``register_authoritative`` is last-wins — which is exactly what
        # re-points a rotated security id on a daily refresh. But a vendor
        # master can list two *different* securities under one trading symbol,
        # which made the owner "whichever row the vendor printed last": Upstox
        # carries NSE_EQ|INE121A01024 (series ``EQ``) and NSE_EQ|INE121A08PJ0
        # (series ``D1``, a debenture) both as ``CHOLAFIN``, and the debenture
        # is printed second — so every request for the equity resolved to a
        # debenture that never trades, an empty series that downstream reads as
        # "this broker has no data for the symbol". Registering
        # weakest-claim-first makes the strongest claimant the owner whatever
        # order the vendor used.
        entries = [
            (self._row_claim_priority(row), row, build_instrument_from_row(row))
            for row in rows
        ]
        # The instrument list keeps master order; only registration is reordered.
        loaded: list[Instrument] = [instrument for _, _, instrument in entries]
        entries.sort(key=lambda entry: entry[0])
        fresh = InstrumentRegistry()
        for _priority, row, instrument in entries:
            exchange = str(row.get("exchange", "NSE")).strip().upper()
            key = str(row.get("key", f"{exchange}:{row.get('symbol', '')}"))
            asset_class = str(row.get("asset_class", "EQUITY")).upper()
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
            incumbent = fresh.provider_key(iid)
            fresh.register_authoritative(iid, key, meta)
            fresh.add_alias(key, iid)
            fresh.add_alias(str(row.get("symbol", "")).strip().upper(), iid)
            self._extra_row_aliases(fresh, row, iid, key)
            if incumbent is not None and incumbent != key:
                # Rows arrive weakest-claim-first, so this one owns the key.
                # A silently re-owned key is how the CHOLAFIN collision went
                # unnoticed for weeks; name it instead. Vendors name the
                # distinguishing field differently — Upstox carries the
                # exchange series in ``instrument_type`` (``EQ``/``D1``), Dhan
                # keeps ``SEM_SERIES`` next to a coarse ``instrument_type`` of
                # ``EQUITY`` — so report whichever the row actually carries.
                kind = str(row.get("series") or native_kind or "").strip()
                log.warning(
                    "master claims %s with more than one key: %s vs %s "
                    "(kind %s) — %s owns the provider key",
                    iid,
                    incumbent,
                    key,
                    kind or "?",
                    key,
                )
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

    def _row_claim_priority(self, row: Mapping[str, Any]) -> int:
        """Hook: how strongly one master row claims its instrument's key.

        When two rows of a single master resolve to the same
        ``InstrumentId``, the higher priority becomes the owner of the primary
        provider key. The default — every row equal — leaves the vendor's own
        row order as the tie-break, so a re-listed security id still re-points
        on refresh. Brokers override this when their master lists materially
        different securities under one trading symbol. A row may carry the
        exchange series as ``series`` (Dhan) or inside ``instrument_type``
        (Upstox); contested symbols are logged with whichever is present.
        """
        return 0

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
    # pass-through wall [G4] — generated from a single declarative spec
    # ------------------------------------------------------------------

    #: Declarative spec for every one-line ``self._transport`` delegation
    #: below. Adding a new broker operation that fits one of the three
    #: standard gate policies (read / mutation / mutation+capability) is a
    #: one-line edit here; non-standard policies (depth, history, search,
    #: capability-only batch list-wraps, etc.) stay as regular methods.
    #:
    #: Each entry is ``(name, gate, arg_names, wraps_list, defaults)``:
    #:   * ``gate`` ∈ ``"read"``, ``"mutation"``, ``"mutation+cap:<name>"``,
    #:     or ``"cap:<name>"``.
    #:   * ``arg_names`` are the parameter names to declare on the generated
    #:     method. The same names are forwarded positionally to the
    #:     transport.
    #:   * ``wraps_list`` is True when the transport returns an iterable and
    #:     the public method must materialise it as a list (preserves the
    #:     pre-refactor ``return list(...)`` semantics).
    #:   * ``defaults`` is a dict of ``name -> repr(default)`` for any
    #:     parameter that must keep a default value (e.g. ``interval=None``).
    _PASSTHROUGH_WALL: tuple[
        tuple[str, str, tuple[str, ...], bool, dict[str, str]], ...
    ] = (
        # orders — read-only
        ("get_order", "read", ("order_id",), False, {}),
        ("get_orderbook", "read", (), True, {}),
        ("get_order_by_correlation_id", "read", ("tag",), False, {}),
        # portfolio — read-only
        ("get_positions", "read", (), True, {}),
        ("get_holdings", "read", (), True, {}),
        ("get_account", "read", (), False, {}),
        ("get_portfolio", "read", (), False, {}),
        # market data — read-only
        ("get_quote", "read", ("instrument",), False, {}),
        ("ltp", "read", ("instrument",), False, {}),
        # orders — mutation-gated
        ("submit_order", "mutation", ("request",), False, {}),
        ("cancel_order", "mutation", ("order_id",), False, {}),
        ("modify_order", "mutation", ("order_id", "request"), False, {}),
        # orders — mutation + capability (super / forever / slice / eDIS)
        (
            "submit_super_order",
            "mutation+cap:supports_super_order",
            ("request",),
            False,
            {},
        ),
        (
            "modify_super_order",
            "mutation+cap:supports_super_order",
            ("order_id", "request"),
            False,
            {},
        ),
        (
            "cancel_super_order",
            "mutation+cap:supports_super_order",
            ("order_id", "leg"),
            False,
            {"leg": "'ENTRY'"},
        ),
        (
            "list_super_orders",
            "cap:supports_super_order",
            (),
            True,
            {},
        ),
        (
            "submit_forever_order",
            "mutation+cap:supports_forever_order",
            ("request",),
            False,
            {},
        ),
        (
            "modify_forever_order",
            "mutation+cap:supports_forever_order",
            ("order_id", "request"),
            False,
            {},
        ),
        (
            "cancel_forever_order",
            "mutation+cap:supports_forever_order",
            ("order_id",),
            False,
            {},
        ),
        (
            "list_forever_orders",
            "cap:supports_forever_order",
            (),
            True,
            {},
        ),
        (
            "submit_slice_order",
            "mutation+cap:supports_slice_order",
            ("request", "slices", "interval"),
            True,
            {"interval": "None"},
        ),
        (
            "submit_edis",
            "mutation+cap:supports_edis",
            ("request",),
            False,
            {},
        ),
        # kill switch
        (
            "kill_switch",
            "mutation+cap:supports_kill_switch",
            ("enable",),
            False,
            {"enable": "True"},
        ),
        ("status_kill_switch", "cap:supports_kill_switch", (), False, {}),
        # auxiliary account surface
        ("exit_all", "mutation", (), False, {}),
        ("profile", "read", (), False, {}),
        ("ledger", "read", ("from_date", "to_date"), True, {}),
        ("fund_limits", "read", (), False, {}),
        ("token_status", "read", (), False, {}),
    )

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # The standard pass-through wall is installed for every subclass too:
        # adapters that don't override a method get the same body. Methods
        # defined on the subclass take precedence and are not overwritten.
        _install_passthrough_wall(cls)

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
    # Capability-gated pass-throughs shared by all
    # adapters that support the flags (super / forever / slice / eDIS,
    # kill switch, auxiliary account surface).
    # ------------------------------------------------------------------

    # All one-line delegations below (super / forever / slice / eDIS,
    # kill switch, auxiliary account surface) are generated by
    # :func:`_install_passthrough_wall` from :attr:`_PASSTHROUGH_WALL`.
    # Custom-policy methods (depth, history, search, ltp/quote_batch,
    # future_chain) keep their hand-written bodies below.
    # ------------------------------------------------------------------

    @property
    def registry(self) -> Any:
        return self._registry


# Install the standard pass-through wall on BaseBroker itself.
# Subclasses get it automatically via ``__init_subclass__``; explicit
# overrides on the class (custom risk policy, broker-specific pass-throughs)
# are preserved because ``_install_passthrough_wall`` skips any name that
# is already in ``cls.__dict__``.
_install_passthrough_wall(BaseBroker)


__all__ = ["BaseBroker"]
