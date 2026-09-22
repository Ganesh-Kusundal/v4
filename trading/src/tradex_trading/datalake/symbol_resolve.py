"""Connect-time universe↔master symbol resolve (ISIN rename aliases).

When the universe CSV lags a corporate rename (HEG→HEGAM, same ISIN), the
master registers the new trading symbol only. This module aliases the old
``InstrumentId`` onto that EQ/BE provider key so ``provider_key`` succeeds.
Symbols that cannot be resolved land in ``quarantine``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from tradex_domain.instruments import Equity

log = logging.getLogger(__name__)

_CASH = frozenset({"EQ", "BE"})


@dataclass
class ResolveResult:
    ok: list[Any] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)  # (old, new)
    quarantine: list[str] = field(default_factory=list)


def _series_of(meta: dict[str, object]) -> str:
    for key in ("series", "instrument_type"):
        raw = meta.get(key)
        if raw:
            return str(raw).strip().upper()
    return ""


def _isin_of(meta: dict[str, object], provider_key: str | None) -> str | None:
    raw = meta.get("isin")
    if raw:
        return str(raw).strip().upper()
    # Upstox cash keys are ``NSE_EQ|<ISIN>``.
    if provider_key and "|" in provider_key:
        tail = provider_key.split("|", 1)[1].strip().upper()
        if tail.startswith("IN") and len(tail) >= 12:
            return tail
    return None


def _isin_index(broker: Any) -> dict[str, tuple[Any, str, str]]:
    """ISIN → (instrument_id, provider_key, trading_symbol) for EQ/BE rows."""
    registry = broker.registry
    loaded = getattr(broker, "_loaded_instruments", None) or []
    out: dict[str, tuple[Any, str, str]] = {}
    for inst in loaded:
        if not isinstance(inst, Equity):
            continue
        iid = inst.instrument_id
        key = registry.provider_key(iid)
        if not key:
            continue
        meta = registry.meta(iid)
        series = _series_of(meta)
        if series and series not in _CASH:
            continue
        isin = _isin_of(meta, key)
        if not isin:
            continue
        # Strongest claim wins if duplicates (EQ preferred via load order).
        out[isin] = (iid, key, inst.symbol)
    return out


def resolve_universe_symbols(broker: Any, instruments: list[Any]) -> ResolveResult:
    """Alias renamed symbols onto EQ/BE provider keys matched by ISIN.

    Returns instruments that are OK to sync (``ok``), rename pairs logged,
    and ``quarantine`` symbols with no usable mapping.
    """
    registry = broker.registry
    by_isin = _isin_index(broker)
    result = ResolveResult()

    for inst in instruments:
        iid = inst.instrument_id
        symbol = getattr(inst, "symbol", str(iid))
        key = registry.provider_key(iid)
        meta = registry.meta(iid) if key else {}
        series = _series_of(meta)

        if key and (not series or series in _CASH):
            result.ok.append(inst)
            continue
        if key and series and series not in _CASH:
            result.quarantine.append(symbol)
            log.warning(
                "symbol_resolve: %s primary is series %s — quarantined",
                symbol, series,
            )
            continue

        isin = getattr(getattr(inst, "meta", None), "isin", None)
        if not isin and getattr(inst, "meta", None) is not None:
            isin = (inst.meta.extra or {}).get("isin")
        isin = str(isin).strip().upper() if isin else None
        hit = by_isin.get(isin) if isin else None
        if hit is None:
            result.quarantine.append(symbol)
            log.warning(
                "symbol_resolve: %s unresolved (isin=%s) — quarantined",
                symbol, isin,
            )
            continue

        new_iid, provider_key, new_symbol = hit
        if new_symbol == symbol:
            result.ok.append(inst)
            continue

        registry.register_authoritative(
            iid, provider_key,
            {"asset_class": "EQUITY", "isin": isin, "series": "EQ",
             "renamed_from": symbol, "renamed_to": new_symbol},
        )
        registry.add_alias(symbol, new_iid)
        result.renamed.append((symbol, new_symbol))
        result.ok.append(inst)
        log.info("symbol_resolve: rename %s → %s (isin=%s)", symbol, new_symbol, isin)

    return result


__all__ = ["ResolveResult", "resolve_universe_symbols"]
