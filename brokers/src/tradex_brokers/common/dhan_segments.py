"""Canonical Dhan exchange-segment tables (audit SMELL-01, order-routing).

Single source of truth for the two Dhan segment key spaces that are *not*
master-CSV pairs and *not* binary wire codes:

* :data:`DHAN_EXCHANGE_SEGMENT` — the **forward** table, canonical v4 domain
  exchange -> Dhan ``ExchangeSegment`` string. This is the order-routing
  table: a wrong value here sends an order to the wrong exchange.
* :data:`DHAN_SEGMENT_EXCHANGE` — its inverse, ``ExchangeSegment`` string ->
  domain exchange, derived by inversion (so it can never desync).

Every value in the forward table is verified against the ``dhanhq`` SDK
exchange constants (``dhanhq.dhanhq``: ``NSE='NSE_EQ'``, ``BSE='BSE_EQ'``,
``FNO='NSE_FNO'``, ``BSE_FNO='BSE_FNO'``, ``MCX='MCX_COMM'``,
``CUR='NSE_CURRENCY'``, ``INDEX='IDX_I'``). Two further keys, ``NSE_COMM`` and
``BCD -> BSE_CURRENCY``, are not standalone SDK constants — Dhan spells
commodity segments by exchange (``NSE_COMM``/``BSE_COMM``) and currency on BSE
as ``BSE_CURRENCY`` — so they are pinned by literal test values instead.

Deliberately **not** here:

* ``dhan/instruments.py:SEGMENT_CANONICAL`` — a different key space
  (``(SEM_EXM_EXCH_ID, SEM_SEGMENT)`` master-CSV character pairs). Do not merge.
* The binary WebSocket wire codes in ``dhan/tick_parser.py``. Those integers
  are Dhan's binary protocol and are **not** derivable from this table. They
  are pinned in :data:`DHAN_WIRE_CODE_EXCHANGE` here so exactly one place
  states them; ``tick_parser`` imports that constant and asserts at import time
  that every code round-trips through the forward table.
"""

from __future__ import annotations

from types import MappingProxyType

#: Canonical v4 domain exchange -> Dhan ``ExchangeSegment`` string.
#: Values verified against the ``dhanhq`` SDK exchange constants (see module
#: docstring). A ``MappingProxyType`` so the table cannot be mutated at runtime
#: by an adapter or test — this is order-routing data.
DHAN_EXCHANGE_SEGMENT: dict[str, str] = dict(
    MappingProxyType(
        {
            "NSE": "NSE_EQ",
            "BSE": "BSE_EQ",
            "NFO": "NSE_FNO",
            "BFO": "BSE_FNO",
            "MCX": "MCX_COMM",
            "NSE_COMM": "NSE_COMM",
            "CDS": "NSE_CURRENCY",
            "BCD": "BSE_CURRENCY",
            "IDX": "IDX_I",
        }
    )
)

#: Inverse of :data:`DHAN_EXCHANGE_SEGMENT`, derived so it cannot desync.
#: One-way: a segment with no forward entry (added by a later SDK release)
#: simply has no reverse entry rather than silently inverting to ``None``.
DHAN_SEGMENT_EXCHANGE: dict[str, str] = {
    segment: exchange for exchange, segment in DHAN_EXCHANGE_SEGMENT.items()
}

#: Dhan's *binary protocol* exchange codes -> canonical v4 domain exchange.
#: An independent protocol fact, NOT derivable from the table above: these
#: integers are the segment code in a WebSocket frame header, whereas the
#: forward table holds REST ``ExchangeSegment`` strings. Pinned to Dhan's
#: documented code list, which also assigns **no segment to code 6** and never
#: sends a code for ``NSE_COMM`` — both gaps are deliberate, so nothing here
#: may fill them in. A wrong entry routes market data to the wrong exchange.
DHAN_WIRE_CODE_EXCHANGE: dict[int, str] = dict(
    MappingProxyType(
        {
            0: "IDX",
            1: "NSE",
            2: "NFO",
            3: "CDS",
            4: "BSE",
            5: "MCX",
            7: "BCD",
            8: "BFO",
        }
    )
)

#: Dhan domain exchange assumed for an unmapped value. Kept here so the REST
#: client and the shared lookup below cannot drift on the fallback.
DEFAULT_DHAN_EXCHANGE = "NSE"
DEFAULT_DHAN_SEGMENT = DHAN_EXCHANGE_SEGMENT[DEFAULT_DHAN_EXCHANGE]


def dhan_segment_for(exchange: object) -> str:
    """Map a domain exchange to a Dhan ``ExchangeSegment`` string.

    Accepts the exchange enum, a plain string, or anything ``str()``-able, and
    normalises case/whitespace. Unmapped values fall back to
    :data:`DEFAULT_DHAN_SEGMENT` — this is the historical behaviour of
    ``dhan.client.dhan_exchange_segment``, preserved exactly.
    """
    value = getattr(exchange, "value", exchange)
    return DHAN_EXCHANGE_SEGMENT.get(str(value).strip().upper(), DEFAULT_DHAN_SEGMENT)


__all__ = [
    "DEFAULT_DHAN_EXCHANGE",
    "DEFAULT_DHAN_SEGMENT",
    "DHAN_EXCHANGE_SEGMENT",
    "DHAN_SEGMENT_EXCHANGE",
    "DHAN_WIRE_CODE_EXCHANGE",
    "dhan_segment_for",
]
