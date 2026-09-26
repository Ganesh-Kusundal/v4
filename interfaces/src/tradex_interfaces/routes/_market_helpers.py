"""Pure transformation helpers for the ``/option-chain`` and ``/future-chain`` routes.

These functions used to live in :mod:`tradex_trading.interface.fastapi_app`
(underscore-prefixed) but they are only used by
:mod:`tradex_trading.interface.routes.market_data`, so they belong in the
market-data module's package. They are pure transformers — no FastAPI or
request-context coupling — so the rename drops the leading underscore.
"""

from __future__ import annotations

from typing import Any


def resolve_underlying_instrument(session: Any, raw: str) -> Any:
    """Resolve a user-provided underlying string to a domain Instrument.

    Accepts ``EXCHANGE:SYMBOL`` (e.g. ``MCX:GOLD``), a registry alias/key,
    or a bare symbol found by searching the loaded master. Raises
    ``LookupError`` when nothing resolves.
    """
    from tradex_brokers.common.provider_common import instrument_from_id
    from tradex_domain.value_objects import InstrumentId

    stripped = raw.strip()
    if "::" not in stripped and ":" in stripped:
        try:
            return instrument_from_id(InstrumentId.parse(stripped))
        except ValueError:
            pass
    broker = getattr(session, "_broker", None)
    registry = getattr(broker, "registry", None)
    if registry is not None:
        iid = registry.resolve(stripped)
        if iid is not None:
            return instrument_from_id(iid)
    try:
        results = list(session.broker.search(stripped))
    except Exception:  # noqa: BLE001 — search is best-effort resolution
        results = []
    for inst in results:
        iid = getattr(inst, "instrument_id", None)
        if iid is not None and iid.underlying.upper() == stripped.upper():
            return inst
    raise LookupError(f"unknown underlying instrument: {stripped!r}")


def serialize_option_chain(chain: Any) -> dict:
    """Serialize an OptionChain into a JSON-friendly dict."""
    expiries = []
    for exp in chain.expiries():
        expiries.append(
            {
                "expiry": exp.expiry_date.isoformat(),
                "reference_price": (
                    str(exp.reference_price.value) if exp.reference_price is not None else None
                ),
                "pairs": [
                    {
                        "strike": str(p.strike.value),
                        "call": str(p.call.instrument_id),
                        "put": str(p.put.instrument_id),
                    }
                    for p in exp.pairs
                ],
            }
        )
    return {"underlying": str(chain.underlying.instrument_id), "expiries": expiries}


def enrich_chain_live(session: Any, chain: Any, max_strikes: int = 11) -> dict:
    """Attach real-time LTP / OI / volume / greeks to the nearest expiry's strikes.

    Best-effort: batch-quotes the ATM-centred strike window (nearest expiry
    first) and attaches ``call_live``/``put_live`` legs per pair. Any quote
    failure degrades to the static chain rather than erroring the request.

    MCX (and other non-NFO/BFO/IDX) underlyings have no REST batch-quote
    endpoint, so OI/volume enrichment is opted out — only the ATM LTP window
    is kept, fetched per-leg and best-effort. Greeks ride in ``quote.metadata``
    when the provider feed carries them (Upstox WS ``option_greeks``).
    """
    expiries = chain.expiries()
    if not expiries:
        return {"underlying": str(chain.underlying.instrument_id), "live": True, "expiries": []}
    underlying_exchange = str(getattr(chain.underlying.exchange, "value", "")).upper()
    batch_supported = underlying_exchange in {"NFO", "BFO", "IDX"}
    target = min(expiries, key=lambda exp: exp.expiry_date)
    reference = target.reference_price.value if target.reference_price is not None else None
    if reference is not None:
        ordered = sorted(target.pairs, key=lambda p: abs(p.strike.value - reference))
    else:
        ordered = list(target.pairs)
    chosen = ordered[:max_strikes]
    instruments = [inst for p in chosen for inst in (p.call, p.put)]
    quotes: dict[Any, Any] = {}
    if batch_supported:
        try:
            quotes = session.broker.quote_batch(instruments)
        except Exception:  # noqa: BLE001 — live enrichment is best-effort
            quotes = {}
    by_id = {str(iid): quote for iid, quote in quotes.items()}
    # ATM-window ids only — the per-leg LTP fallback below must never fan out
    # over the whole chain (MCX has ~1800 pairs; that would be thousands of
    # REST calls).
    chosen_ids = {str(i.instrument_id) for i in instruments}

    def _leg(instrument_id: str, inst: Any) -> dict | None:
        if instrument_id not in chosen_ids:
            return None
        quote = by_id.get(instrument_id)
        if quote is None and not batch_supported:
            # MCX: no REST batch quotes — fetch LTP per-leg (ATM window only).
            try:
                quote = session.broker.ltp(inst)
            except Exception:  # noqa: BLE001 — best-effort LTP
                return None
            return {"ltp": str(quote.value)}
        if quote is None:
            return None
        metadata = quote.metadata or {}
        greeks = metadata.get("greeks")
        return {
            "ltp": str(quote.ltp.value),
            "oi": str(quote.open_interest.value) if quote.open_interest is not None else None,
            "volume": str(quote.volume.value) if quote.volume is not None else None,
            "greeks": dict(greeks) if isinstance(greeks, dict) else None,
        }

    out_expiries = []
    for exp in expiries:
        out_expiries.append(
            {
                "expiry": exp.expiry_date.isoformat(),
                "reference_price": (
                    str(exp.reference_price.value) if exp.reference_price is not None else None
                ),
                "pairs": [
                    {
                        "strike": str(p.strike.value),
                        "call": str(p.call.instrument_id),
                        "put": str(p.put.instrument_id),
                        "call_live": _leg(str(p.call.instrument_id), p.call),
                        "put_live": _leg(str(p.put.instrument_id), p.put),
                    }
                    for p in exp.pairs
                ],
            }
        )
    return {
        "underlying": str(chain.underlying.instrument_id),
        "live": True,
        "expiries": out_expiries,
    }
