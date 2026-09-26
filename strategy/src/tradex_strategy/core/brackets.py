"""Protective levels a strategy declares on its signal, as order legs.

**Where a level belongs.** A stop is a property of the *decision*, not of the
fill pipeline: only the strategy knows why the trade exists and where it stops
being right. So the strategy declares it on the signal it emits, in
``Signal.metadata``, and the engine carries it onto the order. One declaration
then reaches every execution path, because they all build their request here:
the backtest's ``next_open`` queue, live's ``signal_close`` submit, and the
recording-only bridge in ``replay/backtest.py``.

**The contract.** ``metadata['stop_loss_price']`` and
``metadata['target_price']`` — the same names and units ``OrderRequest`` uses, so
nothing has to be translated between the signal and the order. Values may be
``Price``, ``Decimal``, ``int``, ``float`` or a numeric string; a value that is
none of those is dropped with a warning rather than crashing a run, because a
strategy's arithmetic on a thin bar can produce ``None`` or ``NaN`` and the
engine must not die on it.

**Why a pair, and not a lone stop.** The venue's composite (super) endpoint is
the only thing in the stack that understands a protective leg — a plain order's
adapter builds its own payload and would silently discard one. A half-declared
pair is therefore *ignored*, loudly, rather than shipped as a protection the
strategy believes it has. ``BracketOrderRequest`` in the domain encodes the same
rule as a hard invariant.

**Geometry is checked against the fill price, not the signal price.** With
``next_open`` the order fills at the *following* bar's open, which can gap past
the declared target. A pair that no longer brackets the actual entry is dropped
and logged instead of raising mid-run: the venue would reject it anyway, and a
run that dies on bar 4,000 of 5,000 is worse than a run with one unprotected
trade. The dropped pair is visible in the log, never silently applied.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from tradex_domain.enums import OrderSide, OrderType
from tradex_domain.execution import BracketOrderRequest, OrderRequest
from tradex_domain.value_objects import CorrelationId, Price, Quantity

log = logging.getLogger(__name__)

#: Metadata keys, identical to the ``OrderRequest`` fields they feed.
STOP_KEY: Final = "stop_loss_price"
TARGET_KEY: Final = "target_price"


def _as_price(value: Any, key: str) -> Price | None:
    """Normalise a declared level to a ``Price``, or ``None`` if unusable.

    ``NaN``/``inf`` are rejected rather than converted: a non-finite level makes
    every later comparison silently false, which is precisely the failure a
    protective level must not have.
    """
    if value is None:
        return None
    raw: Any = value.value if isinstance(value, Price) else value
    try:
        amount = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        log.warning("signal declared %s=%r, which is not a number; ignoring", key, value)
        return None
    if not amount.is_finite():
        log.warning("signal declared %s=%r, which is not finite; ignoring", key, value)
        return None
    if amount <= 0:
        log.warning("signal declared %s=%r, which is not a price; ignoring", key, value)
        return None
    return Price(value=amount)


def declared_levels(metadata: Any) -> tuple[Price | None, Price | None]:
    """Read ``(stop, target)`` off a signal's metadata. Either may be ``None``."""
    if not isinstance(metadata, dict):
        return (None, None)
    return (
        _as_price(metadata.get(STOP_KEY), STOP_KEY),
        _as_price(metadata.get(TARGET_KEY), TARGET_KEY),
    )


def _brackets(side: OrderSide, entry: Price, stop: Price, target: Price) -> bool:
    """Whether the pair protects *entry* on the side being traded.

    The domain's ``BracketOrderRequest`` enforces this too; checking here makes
    an invalid pair a logged drop instead of an exception thrown from a
    constructor in the middle of a stream.
    """
    if side is OrderSide.BUY:
        return stop.value < entry.value < target.value
    return target.value < entry.value < stop.value


def protective_request(
    *,
    signal: Any,
    entry: Price,
    quantity: Quantity,
    correlation_id: CorrelationId | None = None,
    tag: str | None = None,
    reference_timestamp: Any = None,
    order_type: OrderType = OrderType.MARKET,
) -> OrderRequest:
    """Build the order for *signal*, carrying its declared protective levels.

    Returns a ``BracketOrderRequest`` when the signal declared a usable,
    side-valid pair, and a plain ``OrderRequest`` otherwise — so every caller
    gets the order it would have built before, plus the legs when there are legs
    to carry.
    """
    stop, target = declared_levels(getattr(signal, "metadata", None))
    legs: dict[str, Price] = {}
    if stop is not None and target is not None:
        if _brackets(signal.direction, entry, stop, target):
            legs = {STOP_KEY: stop, TARGET_KEY: target}
        else:
            log.warning(
                "signal %r declared protective levels that do not bracket the entry "
                "(%s=%s, entry=%s, %s=%s); submitting unprotected",
                getattr(signal, "reason", "") or "?",
                STOP_KEY, stop.value, entry.value, TARGET_KEY, target.value,
            )
    elif stop is not None or target is not None:
        log.warning(
            "signal declared only one protective level (%s=%s, %s=%s); the venue "
            "carries stops as a pair, so both are dropped",
            STOP_KEY, stop, TARGET_KEY, target,
        )

    cls = BracketOrderRequest if legs else OrderRequest
    return cls(
        instrument=signal.instrument,
        side=signal.direction,
        order_type=order_type,
        quantity=quantity,
        price=entry,
        correlation_id=correlation_id,
        tag=tag,
        reference_timestamp=reference_timestamp,
        **legs,
    )


