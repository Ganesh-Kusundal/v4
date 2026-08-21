"""Credential-gated live broker construction (F19/F20, N7/N9).

This module is the only v4 boundary that turns environment credentials into a
real provider HTTP fetch. It uses stdlib urllib, keeps secrets in memory only,
and never enables live order placement; the runtime risk gate controls that
separately. Tests inject ``fetch`` and never contact the network.
"""

from __future__ import annotations

import json
import os
import time
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tradex_brokers import DhanBroker, UpstoxBroker
from tradex_brokers.common import (
    MintTokenManager,
    TotpCooldownGuard,
    default_runtime_dir,
    default_token_state_path,
    dhan_totp_mint,
    upstox_refresh_mint,
    upstox_totp_mint,
)
from tradex_brokers.common.endpoints import (
    DHAN_REST_BASE_URL,
    DHAN_SANDBOX_REST_BASE_URL,
    UPSTOX_HFT_BASE_URL,
    UPSTOX_REST_BASE_URL,
    UPSTOX_V3_BASE_URL,
)
from tradex_brokers.common.transport import Fetch
from tradex_brokers.dhan.master import dhan_master_download, parse_dhan_master
from tradex_brokers.upstox.master import parse_upstox_master, upstox_master_download
from tradex_domain import AuthenticationError, SDKError
from tradex_domain.wire import InstrumentRegistry

from tradex_trading.config.env import load_env_file  # noqa: F401 — re-exported for v3 parity
from tradex_trading.runtime.master_lifecycle import MasterFileCache, MasterLoader

JsonFetch = Callable[..., tuple[int, Any]]


def _env(name: str, *, required: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise AuthenticationError(f"missing live credential: {name}")
    return value


def provider_environment(provider: str) -> str:
    """Return the configured provider environment without exposing credentials."""
    return os.environ.get(f"{provider}_ENVIRONMENT", "LIVE").strip().upper()


def _env_with_deprecated_fallback(new_name: str, deprecated_name: str, default: str) -> str:
    """Read ``new_name``; fall back to ``deprecated_name`` with a warning."""
    value = os.environ.get(new_name, "").strip()
    if value:
        return value
    legacy = os.environ.get(deprecated_name, "").strip()
    if legacy:
        warnings.warn(
            f"{deprecated_name} is deprecated; use {new_name} instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return legacy
    return default


# Internal alias retained so factory code reads naturally.
_environment = provider_environment


def _urllib_fetch(timeout: float = 30.0) -> Fetch:
    """Create a JSON HTTP fetch callable; response bodies are decoded only.

    This stdlib fallback is Cloudflare-blocked by ``api.upstox.com`` (error
    1010) because plain urllib lacks a real browser TLS fingerprint; the
    curl_cffi fetch below is preferred whenever it is installed.
    """

    def fetch(method: str, url: str, **kwargs: Any) -> tuple[int, object]:
        headers = {str(k): str(v) for k, v in dict(kwargs.pop("headers", {})).items()}
        params = kwargs.pop("params", None)
        payload = kwargs.pop("json", None)
        raw_data = kwargs.pop("data", None)
        raw_response = bool(kwargs.pop("raw_response", False))
        if params:
            query = urlencode({str(k): str(v) for k, v in dict(params).items()})
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        data: bytes | None = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif raw_data is not None:
            # Form-encoded bodies (OAuth/token endpoints) are passed as ``data``.
            data = raw_data if isinstance(raw_data, bytes) else str(raw_data).encode("utf-8")
        request = Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 — configured broker URL
                raw = response.read()
                body: object = (
                    raw
                    if raw_response
                    else (json.loads(raw.decode("utf-8")) if raw else {})
                )
                return int(response.status), body
        except Exception as exc:  # noqa: BLE001 — preserve status when available
            status = getattr(exc, "code", None)
            if isinstance(status, int):
                raw = exc.read() if hasattr(exc, "read") else b""
                if raw_response:
                    return status, raw
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    body = {"error": "provider request failed"}
                return status, body
            raise SDKError("live provider request failed") from exc

    return fetch  # type: ignore[return-value]


def _curl_cffi_available() -> bool:
    """True when the optional ``curl-cffi`` dependency is importable."""
    try:
        import curl_cffi  # noqa: F401, PLC0415 — optional dependency
    except ImportError:
        return False
    return True


def _curl_cffi_fetch(timeout: float = 30.0) -> Fetch:
    """JSON fetch that impersonates a real Chrome TLS fingerprint (Cloudflare fix).

    ``api.upstox.com`` bans plain urllib with Cloudflare ``error code: 1010``;
    impersonating a browser signature passes the check. Form-encoded bodies
    (OAuth/token endpoints) arrive as ``data``; JSON payloads as ``json``.
    """
    from curl_cffi import requests as cffi_requests  # noqa: PLC0415

    def fetch(method: str, url: str, **kwargs: Any) -> tuple[int, object]:
        headers = {str(k): str(v) for k, v in dict(kwargs.pop("headers", {})).items()}
        params = kwargs.pop("params", None)
        payload = kwargs.pop("json", None)
        raw_data = kwargs.pop("data", None)
        raw_response = bool(kwargs.pop("raw_response", False))
        try:
            response = cffi_requests.request(
                method.upper(),  # type: ignore[arg-type]
                url,
                headers=headers or None,
                params=params,
                json=payload,
                data=raw_data,
                impersonate="chrome131",
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 — surface as SDKError, never leak
            raise SDKError("live provider request failed") from exc
        if raw_response:
            body: object = response.content
        else:
            try:
                body = response.json()
            except ValueError:
                body = {}
        return int(response.status_code), body

    return fetch  # type: ignore[return-value]


def _dhan_master_loader(
    fetch: Fetch,
    *,
    strict: bool,
    cache: MasterFileCache | None = None,
) -> MasterLoader:
    """Dhan master loader: broker-package download+parse, trading-owned cache."""
    return MasterLoader(
        dhan_master_download(fetch, strict=strict),
        lambda raw: parse_dhan_master(raw, strict=strict),
        cache=cache,
    )


def _upstox_master_loader(
    fetch: Fetch,
    *,
    strict: bool,
    cache: MasterFileCache | None = None,
) -> MasterLoader:
    """Upstox master loader: broker-package download+parse, trading-owned cache."""
    return MasterLoader(
        upstox_master_download(fetch, strict=strict),
        lambda raw: parse_upstox_master(raw, strict=strict),
        cache=cache,
    )


def _master_cache(
    provider: str,
    suffix: str,
    *,
    fetch: Fetch | None,
    master_cache_dir: str | Path | None,
) -> MasterFileCache | None:
    """Dated on-disk master cache: explicit dir wins; real fetches default to
    ``v4/runtime``; injected fetches (tests) stay cache-free unless a dir is
    given explicitly."""
    if master_cache_dir is not None:
        root = Path(master_cache_dir)
    elif fetch is None:
        root = default_runtime_dir()
    else:
        return None
    return MasterFileCache(root, prefix=f"{provider}-instruments-", suffix=suffix)


def resolve_fetch(timeout: float = 30.0) -> Fetch:
    """Return a live fetch, preferring curl_cffi (browser impersonation) when the
    optional dependency is installed and falling back to stdlib urllib."""
    if _curl_cffi_available():
        return _curl_cffi_fetch(timeout)
    return _urllib_fetch(timeout)


def _dhan_base(environment: str) -> str:
    if environment == "SANDBOX":
        return os.environ.get(
            "DHAN_SANDBOX_REST_BASE_URL", DHAN_SANDBOX_REST_BASE_URL
        ).rstrip("/")
    return _env_with_deprecated_fallback(
        "DHAN_REST_BASE_URL", "DHAN_BASE_URL", DHAN_REST_BASE_URL
    ).rstrip("/")


def _upstox_bases(environment: str) -> tuple[str, str, str]:
    prefix = "UPSTOX_SANDBOX_" if environment == "SANDBOX" else "UPSTOX_"
    base_url = os.environ.get(f"{prefix}BASE_URL", UPSTOX_REST_BASE_URL).rstrip("/")
    if environment == "SANDBOX":
        base_hft = os.environ.get("UPSTOX_SANDBOX_BASE_HFT", UPSTOX_HFT_BASE_URL)
        base_v3 = os.environ.get("UPSTOX_SANDBOX_BASE_V3", UPSTOX_V3_BASE_URL)
    else:
        base_hft = _env_with_deprecated_fallback(
            "UPSTOX_BASE_HFT", "UPSTOX_HFT_BASE_URL", UPSTOX_HFT_BASE_URL
        )
        base_v3 = _env_with_deprecated_fallback(
            "UPSTOX_BASE_V3", "UPSTOX_V3_BASE_URL", UPSTOX_V3_BASE_URL
        )
    return base_url, base_hft.rstrip("/"), base_v3.rstrip("/")


def _cooldown_guard(
    broker: str, env_var: str, injected: TotpCooldownGuard | None
) -> TotpCooldownGuard:
    """Resolve the TOTP cooldown guard, honoring a shared-state env override.

    ``*_COOLDOWN_PATH`` points the durable guard at a shared file (e.g.
    ``~/.dhan/dhan-totp-cooldown.json``) so sibling processes/projects honor
    the same broker-side rate limit instead of each keeping a private one.
    """
    if injected is not None:
        return injected
    path = os.environ.get(env_var, "").strip()
    if path:
        return TotpCooldownGuard(broker=broker, state_path=Path(path).expanduser())
    return TotpCooldownGuard.for_broker(broker)


def _dhan_token_manager(
    fetch: Fetch,
    client_id: str,
    *,
    cooldown: TotpCooldownGuard | None = None,
    clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], None] = time.sleep,
) -> MintTokenManager | None:
    """Build a durable Dhan TOTP manager when PIN+TOTP credentials are present.

    Defaults ``DHAN_TOKEN_PATH`` to a durable ``runtime/dhan-token-state.json``
    (G4) so probe-before-mint reuses a valid token across restarts, and wires a
    real 120s cooldown guard (G3) unless the caller injects one (tests).

    Returns a manager even when the mint is a stub — the manager can still
    reuse a valid persisted token from disk until it expires.
    """
    pin = os.environ.get("DHAN_PIN", "").strip()
    totp_secret = os.environ.get("DHAN_TOTP_SECRET", "").strip()
    if not (client_id and pin and totp_secret):
        return None
    try:
        buffer_minutes = float(os.environ.get("DHAN_REFRESH_BUFFER_MINUTES", "10"))
    except ValueError:
        buffer_minutes = 10.0
    token_path = os.environ.get("DHAN_TOKEN_PATH") or str(default_token_state_path("dhan"))
    # Real stdlib TOTP mint (v3 parity): POSTs client id + pin + TOTP to mint a
    # fresh access token on demand. The factory is cheap (no network); the POST
    # happens only when the persisted token is missing/expired/rejected.
    mint_fn: Callable[[], Any] = dhan_totp_mint(
        fetch=cast(JsonFetch, fetch),
        client_id=client_id,
        pin=pin,
        totp_secret=totp_secret,
        clock=clock,
        sleeper=sleeper,
    )
    return MintTokenManager(
        state_path=token_path,
        mint=mint_fn,
        refresh_buffer_seconds=buffer_minutes * 60.0,
        clock=clock,
        cooldown=_cooldown_guard("dhan", "DHAN_COOLDOWN_PATH", cooldown),
    )


def _upstox_token_manager(
    fetch: Fetch,
    environment: str,
    *,
    cooldown: TotpCooldownGuard | None = None,
    clock: Callable[[], float] = time.time,
) -> MintTokenManager | None:
    """Build a durable Upstox token manager.

    Three-tier mint priority (mirrors v2):
      1. OAuth refresh-token grant (``UPSTOX_REFRESH_TOKEN``)
      2. TOTP self-mint via ``upstox-totp``
         (``UPSTOX_MOBILE`` + ``UPSTOX_PIN`` + ``UPSTOX_TOTP_SECRET``)
      3. Static access token (``UPSTOX_ACCESS_TOKEN`` — no refresh capability)

    Defaults the token path to durable ``runtime/upstox-token-state.json`` (G4)
    and wires a 600s cooldown guard (G3) unless a guard is injected.
    """
    prefix = "UPSTOX_SANDBOX_" if environment == "SANDBOX" else "UPSTOX_"
    client_id = (
        os.environ.get(f"{prefix}API_KEY") or os.environ.get(f"{prefix}CLIENT_ID") or ""
    ).strip()
    client_secret = (
        os.environ.get(f"{prefix}API_SECRET") or os.environ.get(f"{prefix}CLIENT_SECRET") or ""
    ).strip()
    _redirect_uri = os.environ.get(
        f"{prefix}REDIRECT_URI", "http://127.0.0.1:18080/callback"
    ).strip()
    env_refresh = os.environ.get(f"{prefix}REFRESH_TOKEN", "").strip()

    # TOTP self-mint credentials (v2 parity).
    totp_mobile = os.environ.get("UPSTOX_MOBILE", "").strip()
    totp_pin = os.environ.get("UPSTOX_PIN", "").strip()
    totp_secret = os.environ.get("UPSTOX_TOTP_SECRET", "").strip()
    has_totp = bool(totp_mobile and totp_pin and totp_secret and client_id and client_secret)

    token_path = os.environ.get(f"{prefix}TOKEN_PATH") or str(default_token_state_path("upstox"))
    guard = _cooldown_guard("upstox", f"{prefix}COOLDOWN_PATH", cooldown)

    # Tier 1: OAuth refresh-token grant.  The factory is network-free; the POST
    # happens only when the persisted token is missing/expired/rejected.
    if env_refresh and client_id and client_secret:

        def build(refresh: str) -> MintTokenManager:
            return MintTokenManager(
                state_path=token_path,
                mint=upstox_refresh_mint(
                    fetch=cast(JsonFetch, fetch),
                    client_id=client_id,
                    client_secret=client_secret,
                    redirect_uri=_redirect_uri,
                    refresh_token=refresh,
                    clock=clock,
                ),
                refresh_buffer_seconds=1800.0,
                clock=clock,
                cooldown=guard,
            )

        manager = build(env_refresh)
        # Upstox rotates refresh tokens — prefer the latest persisted one.
        persisted = manager.persisted_refresh_token()
        return build(persisted) if persisted and persisted != env_refresh else manager

    # Tier 2: TOTP self-mint (requires the optional ``upstox-totp`` package;
    # its absence surfaces as a typed AuthenticationError on first mint).
    if has_totp:
        return MintTokenManager(
            state_path=token_path,
            mint=upstox_totp_mint(
                mobile=totp_mobile,
                pin=totp_pin,
                totp_secret=totp_secret,
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=_redirect_uri,
                clock=clock,
            ),
            refresh_buffer_seconds=1800.0,
            clock=clock,
            cooldown=guard,
        )

    # No mint strategy available — caller falls back to static access token.
    return None


def build_dhan_from_env(
    *,
    fetch: Fetch | None = None,
    registry: InstrumentRegistry | None = None,
    expected_environment: str | None = None,
    allow_order_operations: bool = False,
    timeout: float = 30.0,
    cache_ttl_seconds: float = 0.0,
    cooldown: TotpCooldownGuard | None = None,
    load_instruments: bool | None = None,
    master_cache_dir: str | Path | None = None,
    token_clock: Callable[[], float] | None = None,
    token_sleeper: Callable[[float], None] | None = None,
) -> DhanBroker:
    """Build a Dhan broker from env credentials; no network call occurs yet.

    ``token_clock`` / ``token_sleeper`` (test-only) override the wall clock
    and sleep used by the durable token manager and its TOTP mint so tests
    can prove tokens are minted only when missing/expired/401-rejected.
    """
    environment = _environment("DHAN")
    if environment not in {"LIVE", "SANDBOX"}:
        raise SDKError(f"unsupported DHAN_ENVIRONMENT: {environment!r}")
    if expected_environment is not None and environment != expected_environment.strip().upper():
        raise SDKError(
            f"DHAN_ENVIRONMENT {environment!r} does not match runtime "
            f"environment {expected_environment!r}"
        )
    client_id = (
        _env("DHAN_SANDBOX_CLIENT_ID") if environment == "SANDBOX" else _env("DHAN_CLIENT_ID")
    )
    resolved_fetch = fetch or resolve_fetch(timeout)
    token_manager = _dhan_token_manager(
        resolved_fetch,
        client_id,
        cooldown=cooldown,
        clock=token_clock or time.time,
        sleeper=token_sleeper or time.sleep,
    )
    # A static token is only required when no TOTP manager can mint on demand.
    token = ""
    if token_manager is None:
        token = (
            _env("DHAN_SANDBOX_ACCESS_TOKEN")
            if environment == "SANDBOX"
            else _env("DHAN_ACCESS_TOKEN")
        )
    # Build the master loader before the adapter so it can be passed through
    # the declared ``instrument_loader`` hook (no post-hoc private assignment).
    if load_instruments is not False:
        # Pass the *original* fetch: None means a real (non-injected) connect,
        # which defaults the cache to the durable runtime dir. An injected
        # fetch (tests) stays cache-free unless master_cache_dir is given.
        _dhan_cache = _master_cache(
            "dhan", ".json", fetch=fetch, master_cache_dir=master_cache_dir
        )
        _dhan_loader = _dhan_master_loader(resolved_fetch, strict=True, cache=_dhan_cache)
        instrument_loader: Callable[[], list[dict[str, Any]]] | None = _dhan_loader.load
    else:
        _dhan_loader = None
        instrument_loader = None
    broker = DhanBroker.from_fetch(
        fetch=resolved_fetch,
        registry=registry,
        client_id=client_id,
        access_token=token,
        token_manager=token_manager,
        base_url=_dhan_base(environment),
        allow_order_operations=allow_order_operations,
        instrument_loader=instrument_loader,
    )
    # Attach optional metadata for scheduler / daily-refresh hook.
    broker.master_loader = _dhan_loader
    # Wire WebSocket market-data stream backend through the adapter's declared
    # surface (no transport reach-in).
    broker._ws_backend = broker.market_stream_backend()
    return broker


def build_upstox_from_env(
    *,
    fetch: Fetch | None = None,
    registry: InstrumentRegistry | None = None,
    expected_environment: str | None = None,
    allow_order_operations: bool = False,
    timeout: float = 30.0,
    cache_ttl_seconds: float = 0.0,
    cooldown: TotpCooldownGuard | None = None,
    load_instruments: bool | None = None,
    master_cache_dir: str | Path | None = None,
    token_clock: Callable[[], float] | None = None,
) -> UpstoxBroker:
    """Build an Upstox broker from env credentials; no network call occurs yet.

    ``token_clock`` (test-only) overrides the wall clock used by the durable
    token manager and its mint so tests can prove tokens are minted only when
    missing/expired/401-rejected.
    """
    environment = _environment("UPSTOX")
    if environment not in {"LIVE", "SANDBOX"}:
        raise SDKError(f"unsupported UPSTOX_ENVIRONMENT: {environment!r}")
    if expected_environment is not None and environment != expected_environment.strip().upper():
        raise SDKError(
            f"UPSTOX_ENVIRONMENT {environment!r} does not match runtime "
            f"environment {expected_environment!r}"
        )
    base_url, base_hft, base_v3 = _upstox_bases(environment)
    resolved_fetch = fetch or resolve_fetch(timeout)
    token_manager = _upstox_token_manager(
        resolved_fetch,
        environment,
        cooldown=cooldown,
        clock=token_clock or time.time,
    )
    # A static token is only required when no refresh manager can mint on demand.
    token = ""
    if token_manager is None:
        token = (
            _env("UPSTOX_SANDBOX_ACCESS_TOKEN")
            if environment == "SANDBOX"
            else _env("UPSTOX_ACCESS_TOKEN")
        )
    # Build the master loader before the adapter so it can be passed through
    # the declared ``instrument_loader`` hook (no post-hoc private assignment).
    if load_instruments is not False:
        _upstox_cache = _master_cache(
            "upstox", ".json", fetch=fetch, master_cache_dir=master_cache_dir
        )
        _upstox_loader = _upstox_master_loader(resolved_fetch, strict=True, cache=_upstox_cache)
        instrument_loader: Callable[[], list[dict[str, Any]]] | None = _upstox_loader.load
    else:
        _upstox_loader = None
        instrument_loader = None
    broker = UpstoxBroker.from_fetch(
        fetch=resolved_fetch,
        registry=registry,
        access_token=token,
        token_manager=token_manager,
        base_url=base_url,
        base_hft=base_hft,
        base_v3=base_v3,
        allow_order_operations=allow_order_operations,
        instrument_loader=instrument_loader,
    )
    # Attach optional metadata for scheduler / daily-refresh hook.
    broker.master_loader = _upstox_loader
    # Wire WebSocket market-data stream backend through the adapter's declared
    # surface: the transport materializes the real UpstoxMarketDataStreamBackend
    # when a WS transport is bound (no transport reach-in here).
    broker._ws_backend = broker.market_stream_backend()
    return broker


def build_broker_from_env(
    provider: str,
    *,
    fetch: Fetch | None = None,
    registry: InstrumentRegistry | None = None,
    expected_environment: str | None = None,
    allow_order_operations: bool = False,
    timeout: float = 30.0,
    cache_ttl_seconds: float = 0.0,
    cooldown: TotpCooldownGuard | None = None,
    load_instruments: bool | None = None,
    master_cache_dir: str | Path | None = None,
    token_clock: Callable[[], float] | None = None,
    token_sleeper: Callable[[float], None] | None = None,
) -> DhanBroker | UpstoxBroker:
    """Build the named live broker; paper remains owned by ``runtime.boot``."""
    name = provider.strip().lower()
    if name == "dhan":
        return build_dhan_from_env(
            fetch=fetch,
            registry=registry,
            expected_environment=expected_environment,
            allow_order_operations=allow_order_operations,
            timeout=timeout,
            cache_ttl_seconds=cache_ttl_seconds,
            cooldown=cooldown,
            load_instruments=load_instruments,
            master_cache_dir=master_cache_dir,
            token_clock=token_clock,
            token_sleeper=token_sleeper,
        )
    if name == "upstox":
        return build_upstox_from_env(
            fetch=fetch,
            registry=registry,
            expected_environment=expected_environment,
            allow_order_operations=allow_order_operations,
            timeout=timeout,
            cache_ttl_seconds=cache_ttl_seconds,
            cooldown=cooldown,
            load_instruments=load_instruments,
            master_cache_dir=master_cache_dir,
            token_clock=token_clock,
        )
    raise SDKError(f"unsupported live broker: {provider!r}")


__all__ = [
    "build_broker_from_env",
    "build_dhan_from_env",
    "build_upstox_from_env",
    "load_env_file",
    "provider_environment",
    "resolve_fetch",
]
