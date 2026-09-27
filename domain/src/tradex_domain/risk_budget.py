"""Per-strategy risk budgets and the fail-closed limit vocabulary.

``RiskBudget`` lives in ``domain`` rather than in ``tradex_execution`` because
``config`` must be able to carry the budgets on ``AppConfig.risk.budgets`` and
``config`` is not allowed to import ``execution`` (the layering rule in
``tests/test_import_boundaries.py``). ``tradex_execution.risk`` re-exports this
class so the pre-existing import path keeps working.

The required-limit vocabulary also lives here so ``config`` (which declares the
fields) and ``runtime`` (which enforces them at boot) speak the same names
without either importing the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

#: The explicit, documented "this limit is intentionally unbounded" value.
#:
#: AC-10 says no risk limit may be *optional*; it does not say an operator may
#: never choose to run unbounded. This sentinel is that choice made explicit. A
#: limit left at ``None`` is *unset* and refuses boot; a limit set to
#: ``UNBOUNDED`` is a deliberate decision and boots. Because a money-path
#: default must never be a silent one, ``None`` is never read as unbounded — the
#: boot gate turns it into a refusal instead.
UNBOUNDED = Decimal("-1")


def is_unbounded(value: Decimal | int | None) -> bool:
    """True when *value* is the explicit ``UNBOUNDED`` opt-out.

    ``None`` is NOT unbounded here — it is unset, and the boot gate refuses on
    it. Callers that want "missing" to mean "unbounded" are the defect this
    whole mechanism exists to remove.
    """
    if value is None:
        return False
    return Decimal(str(value)) == UNBOUNDED


#: The five ``RiskConfig`` limits that must be set (or explicitly opted out of)
#: before a paper or live session may boot. Order is the reporting order: a
#: refusal names every missing limit, not just the first.
REQUIRED_RISK_LIMITS: tuple[str, ...] = (
    "max_order_value",
    "max_position_value",
    "max_orders_per_minute",
    "max_daily_loss_amt",
    "max_drawdown_pct",
)


@dataclass(frozen=True, slots=True)
class RiskBudget:
    """Per-strategy slice of the broker-level risk envelope (AC-9).

    Each strategy on the same account draws from its own budget so a buggy
    strategy on instrument A can never exhaust the limit for strategy B on
    instrument C. A strategy with no budget falls back to the global caps on
    ``RiskManager``; that fallback is exactly the thing AC-10 removes at boot,
    where an operator must name a budget for every strategy the session trades.

    The limits are ``Decimal | None``; ``None`` here means "this strategy has no
    opinion on this particular cap", which is distinct from ``UNBOUNDED``
    (an explicit unlimited choice) and distinct from the ``RiskConfig`` level
    ``None`` (unset, which refuses boot).
    """

    strategy_id: str
    max_order_value: Decimal | None = None
    max_position_value: Decimal | None = None
    max_daily_loss_amt: Decimal | None = None
    max_drawdown_pct: Decimal | None = None


def resolve_strategy_id(request: object) -> str | None:
    """Return the strategy identifier that produced *request*, or ``None``.

    Two routes, in order of authority:

    1. ``request.strategy_id`` — the real field the AC-9 data model adds to
       ``Order``/``OrderRequest``. Read with ``getattr`` so this function is
       correct whether or not that field has landed yet; before the merge it is
       simply ``None`` and step 2 answers instead.
    2. ``request.tag`` — the pre-existing carrier, which the strategy engine
       stamps as ``"<strategy_id>@<version>"`` (``ReactiveStrategyEngine``
       writes ``f"{strategy_id}@{version}"``). The bare version is *not* part
       of the identity, so the text before the first ``@`` is the strategy id.
       A tag with no ``@`` is already a bare id and is used as-is.

    Returns ``None`` when neither route yields a non-empty identifier — a
    request with no attributable strategy falls back to the global caps, which
    is the legacy behaviour and the one ``RiskBudget`` documentation calls out.
    """
    sid = getattr(request, "strategy_id", None)
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    tag = getattr(request, "tag", None)
    if not isinstance(tag, str):
        return None
    bare = tag.split("@", 1)[0].strip()
    return bare or None


__all__ = [
    "REQUIRED_RISK_LIMITS",
    "UNBOUNDED",
    "RiskBudget",
    "is_unbounded",
    "resolve_strategy_id",
]
