"""Gateway configuration read from the environment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_KNOWN_BROKERS = frozenset({"dhan", "upstox"})


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    return int(value)


def _as_float(value: str | None, default: float) -> float:
    if value is None or value.strip() == "":
        return default
    return float(value)


def _as_names(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None or value.strip() == "":
        return default
    return tuple(part.strip().lower() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8100
    api_key: str | None = None
    allow_anon: bool = False
    brokers: tuple[str, ...] = ("dhan", "upstox")
    primary_live: str = "dhan"
    primary_quotes: str = "upstox"
    primary_history: str = "upstox"
    primary_depth: str = "dhan"
    primary_chain: str = "dhan"
    rest_rate_per_second: float = 20.0
    rest_burst: int = 40
    max_subscriptions: int = 1000
    stale_after_seconds: float = 15.0
    broker_workers: int = 8
    master_cache_dir: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GatewayConfig:
        import os

        source = os.environ if env is None else env
        brokers = _as_names(source.get("GATEWAY_BROKERS"), ("dhan", "upstox"))
        primary_live = (source.get("GATEWAY_PRIMARY_LIVE") or "dhan").strip().lower()
        primary_quotes = (source.get("GATEWAY_PRIMARY_QUOTES") or "upstox").strip().lower()
        primary_history = (source.get("GATEWAY_PRIMARY_HISTORY") or "upstox").strip().lower()
        primary_depth = (source.get("GATEWAY_PRIMARY_DEPTH") or "dhan").strip().lower()
        primary_chain = (source.get("GATEWAY_PRIMARY_CHAIN") or "dhan").strip().lower()
        return cls(
            host=(source.get("GATEWAY_HOST") or "127.0.0.1").strip(),
            port=_as_int(source.get("GATEWAY_PORT"), 8100),
            api_key=(source.get("GATEWAY_API_KEY") or None),
            allow_anon=_as_bool(source.get("GATEWAY_ALLOW_ANON"), False),
            brokers=brokers,
            primary_live=primary_live,
            primary_quotes=primary_quotes,
            primary_history=primary_history,
            primary_depth=primary_depth,
            primary_chain=primary_chain,
            rest_rate_per_second=_as_float(source.get("GATEWAY_REST_RATE_PER_SECOND"), 20.0),
            rest_burst=_as_int(source.get("GATEWAY_REST_BURST"), 40),
            max_subscriptions=_as_int(source.get("GATEWAY_MAX_SUBSCRIPTIONS"), 1000),
            stale_after_seconds=_as_float(source.get("GATEWAY_STALE_AFTER_SECONDS"), 15.0),
            broker_workers=_as_int(source.get("GATEWAY_BROKER_WORKERS"), 8),
            master_cache_dir=(source.get("GATEWAY_MASTER_CACHE_DIR") or None),
        )

    def validate(self) -> GatewayConfig:
        if not self.brokers:
            raise ValueError("GATEWAY_BROKERS must name at least one broker")
        unknown = sorted(set(self.brokers) - _KNOWN_BROKERS)
        if unknown:
            raise ValueError(f"unknown broker(s): {', '.join(unknown)}")
        if self.port <= 0 or self.port > 65535:
            raise ValueError(f"invalid port: {self.port}")
        if self.rest_rate_per_second <= 0:
            raise ValueError("GATEWAY_REST_RATE_PER_SECOND must be > 0")
        if self.rest_burst <= 0:
            raise ValueError("GATEWAY_REST_BURST must be > 0")
        if self.max_subscriptions <= 0:
            raise ValueError("GATEWAY_MAX_SUBSCRIPTIONS must be > 0")
        if self.stale_after_seconds <= 0:
            raise ValueError("GATEWAY_STALE_AFTER_SECONDS must be > 0")
        if self.broker_workers <= 0:
            raise ValueError("GATEWAY_BROKER_WORKERS must be > 0")
        loopback = self.host in _LOOPBACK_HOSTS
        if not loopback and not self.api_key:
            raise ValueError("a non-loopback bind requires GATEWAY_API_KEY")
        if self.allow_anon and not loopback:
            raise ValueError("GATEWAY_ALLOW_ANON is only allowed on a loopback bind")
        if self.api_key is None and not self.allow_anon:
            raise ValueError(
                "GATEWAY_API_KEY is required (or set GATEWAY_ALLOW_ANON=true on loopback)"
            )
        # A primary that is not enabled silently falls back to the first broker.
        return replace(
            self,
            primary_live=self._enabled(self.primary_live),
            primary_quotes=self._enabled(self.primary_quotes),
            primary_history=self._enabled(self.primary_history),
            primary_depth=self._enabled(self.primary_depth),
            primary_chain=self._enabled(self.primary_chain),
        )

    def _enabled(self, name: str) -> str:
        return name if name in self.brokers else self.brokers[0]

    def primary_for(self, capability: str) -> str:
        return {
            "live": self.primary_live,
            "quotes": self.primary_quotes,
            "history": self.primary_history,
            "depth": self.primary_depth,
            "chain": self.primary_chain,
            "instruments": self.primary_history,
        }.get(capability, self.brokers[0])


def load_local_env(path: str | Path = ".env.local") -> tuple[str, ...]:
    """Load a local credential file if it exists; never create or print it."""
    from tradex_config.env import load_env_file

    candidate = Path(path)
    if not candidate.is_file():
        return ()
    return load_env_file(candidate)
