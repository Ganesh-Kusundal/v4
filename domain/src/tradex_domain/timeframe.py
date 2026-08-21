"""Single-source timeframe registry (REF-02).

Previously every consumer re-declared its own mapping:

  domain/market.py:_bucketize  seconds dict
  brokers/dhan/_marketdata     interval_map + requested_timeframe
  brokers/upstox/_marketdata   _unit_interval + _target_timeframe
  trading/datalake/parallel_fetcher  _DHAN_INTRADAY_TIMEFRAMES

Ponytail: one dict of dicts, typed, stdlib only.  Dhan lacks M30 — its
interval is ``None`` so callers fail at the registry, not at the wire.

Dhan poll caps (parallel_fetcher guards) also live here via
``MAX_POLL_DAYS[(provider, timeframe)]`` — the only place that knows
``Upstox M1→30d`` vs ``Dhan M1→90d``.
"""

from __future__ import annotations

from tradex_domain.enums import Timeframe

# canon -> {dhan_interval, upstox_unit, upstox_interval, bucket_seconds, is_intraday}
_TIMEFRAMES: dict[str, dict[str, object]] = {
    "1m": {"dhan": "1", "upstox_unit": "minutes", "upstox_interval": "1", "seconds": 60, "intraday": True},
    "5m": {"dhan": "5", "upstox_unit": "minutes", "upstox_interval": "5", "seconds": 300, "intraday": True},
    "15m": {"dhan": "15", "upstox_unit": "minutes", "upstox_interval": "15", "seconds": 900, "intraday": True},
    "30m": {"dhan": None, "upstox_unit": "minutes", "upstox_interval": "30", "seconds": 1800, "intraday": True},
    "1h": {"dhan": "60", "upstox_unit": "hours", "upstox_interval": "1", "seconds": 3600, "intraday": True},
    "1d": {"dhan": None, "upstox_unit": "days", "upstox_interval": "1", "seconds": 86400, "intraday": False},
    "1w": {"dhan": None, "upstox_unit": "weeks", "upstox_interval": "1", "seconds": 604800, "intraday": False},
}

_TF_BY_VALUE: dict[str, Timeframe] = {
    "1m": Timeframe.M1, "5m": Timeframe.M5, "15m": Timeframe.M15,
    "30m": Timeframe.M30, "1h": Timeframe.H1, "1d": Timeframe.D1, "1w": Timeframe.W1,
}

_SECONDS_BY_TF: dict[Timeframe, int] = {
    Timeframe.M1: 60, Timeframe.M5: 300, Timeframe.M15: 900, Timeframe.M30: 1800,
    Timeframe.H1: 3600, Timeframe.D1: 86400, Timeframe.W1: 604800,
}

DHAN_INTRADAY: frozenset[Timeframe] = frozenset({Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1})
UPSTOX_MINUTE: frozenset[Timeframe] = frozenset({Timeframe.M1, Timeframe.M5, Timeframe.M15})


def normalize(value: Timeframe | str) -> tuple[str, Timeframe]:
    """Return (canonical_str, Timeframe) for ``str | Timeframe``."""
    raw = value.value if isinstance(value, Timeframe) else str(value)
    tf = _TF_BY_VALUE.get(raw)
    if tf is None:
        raise ValueError(f"unsupported timeframe: {value!r}")
    return raw, tf


def bucket_seconds(tf: Timeframe) -> int:
    """Seconds per bar for resample/bucketize."""
    return _SECONDS_BY_TF[tf]


def dhan_interval(value: Timeframe | str) -> str | None:
    """Dhan ``interval`` string (``None`` if unsupported, e.g. M30)."""
    raw, _ = normalize(value)
    return _TIMEFRAMES[raw]["dhan"]  # type: ignore[return-value]


def upstox_unit_interval(value: Timeframe | str) -> tuple[str, str]:
    """Upstox ``(unit, interval)`` pair."""
    raw, _ = normalize(value)
    row = _TIMEFRAMES[raw]
    return str(row["upstox_unit"]), str(row["upstox_interval"])


def to_timeframe(value: Timeframe | str) -> Timeframe:
    """Canonical ``Timeframe`` for any accepted value."""
    _, tf = normalize(value)
    return tf


__all__ = [
    "DHAN_INTRADAY",
    "UPSTOX_MINUTE",
    "bucket_seconds",
    "dhan_interval",
    "normalize",
    "to_timeframe",
    "upstox_unit_interval",
]
