"""The AC-17 safety surface: everything ``GET /api/charts/safety`` reports.

The route is a thin shell over this module. Two rules shape it.

**Severity is derived, never read.** :func:`derive_severity` takes the
*observed facts* of a discrepancy — a quantity that disagrees, a status that
disagrees, a funds difference, a side the venue could not be read for — and
computes the severity from them. It never consults
``tradex_execution.reconciliation.DriftItem.severity``, which is a stored field
whose existing values contradict AC-17 (it has a ``MEDIUM`` AC-17 does not, and
calls a status mismatch ``LOW`` where AC-17 calls it ``HIGH``). A caller that
passed that field in would be laundering a stored verdict into a derived one,
which is the whole thing AC-17 is written against. :class:`DriftFact` has no
``severity`` attribute, so the omission is structural rather than a rule to
remember.

**Unknown is not zero.** A session that has never run a reconciliation reports
``last_checked_at = None`` and every severity count at zero, and
:attr:`SafetyReport.boot_refused` is ``False`` — but so is
:attr:`SafetyReport.reconciliation_checked``. A caller deciding whether it may
trade reads the second, not the first: a never-checked reconciliation has no
CRITICAL drift because nothing looked, not because there is none.

Nothing here imports FastAPI. The module is testable without a session and
without an app, which is what makes the severity rule unit-testable per branch.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from tradex_execution.reconciliation import DriftItem

#: Funds differences at or below this are noise from paise-level rounding, not
#: drift. AC-17: "a funds difference above one rupee is HIGH".
FUNDS_DRIFT_THRESHOLD_INR: Decimal = Decimal("1")

#: Severities that refuse boot, named once. AC-17: "only HIGH and CRITICAL
#: refuse boot" — LOW does not, and a LOW-only reconciliation must let a
#: session start.
BOOT_REFUSING_SEVERITIES: frozenset[str] = frozenset({"HIGH", "CRITICAL"})

#: Every severity the surface reports, in ascending order. The response carries
#: a count for each whether or not it is zero, so a consumer can render a row
#: without inventing the missing ones.
SEVERITIES: tuple[str, ...] = ("LOW", "HIGH", "CRITICAL")


class DriftKind(StrEnum):
    """The three things a reconciliation compares.

    A closed set because the severity rule is exhaustive over it: a fourth kind
    would need a fourth branch, and a branch that silently defaulted to LOW is
    the failure this module exists to prevent.
    """

    POSITION = "position"
    ORDER = "order"
    FUNDS = "funds"


@dataclass(frozen=True, slots=True)
class DriftFact:
    """What was *observed* about one discrepancy, and nothing else.

    Every field is a measurement or a presence flag. There is deliberately no
    ``severity`` field: :func:`derive_severity` computes it, so no caller can
    assert a verdict the rule would not have reached.
    """

    kind: DriftKind
    #: A quantity disagreement — order quantity, filled quantity, or a
    #: position's local-versus-venue net. ``False`` when the two agree.
    quantity_mismatch: bool = False
    #: A status disagreement — order status, including a venue status the local
    #: book has not caught up with. ``False`` when the two agree.
    status_mismatch: bool = False
    #: Absolute funds difference in rupees, for :attr:`DriftKind.FUNDS`. ``0.0``
    #: when there is no funds difference to measure.
    funds_difference: Decimal = Decimal("0")
    #: The venue could not be read for this key — the local book has a row the
    #: venue has no answer for, or vice versa. True means *absent*, not *zero*:
    #: the reading is missing, which is why it is CRITICAL rather than HIGH.
    venue_read_missing: bool = False


def derive_severity(fact: DriftFact) -> str:
    """The severity AC-17 defines for one observed discrepancy.

    The rule, in the order it is applied, and why that order:

    * **A missing venue read is CRITICAL.** Checked first because it is the
      most severe and because a missing read can *also* look like a quantity
      disagreement — the venue side is zero precisely because nothing came
      back. Reading the missing read as "we hold 100, they hold 0" would file
      the only genuinely unknown state as a plain HIGH and let it start a
      session.
    * **A quantity or status mismatch is HIGH.** Both are the book
      disagreeing with the venue about something that decides what is held.
    * **A funds difference above one rupee is HIGH.** Strictly above: exactly
      one rupee is not above one rupee, and is LOW.
    * **Anything below is LOW.** The floor, not a verdict about safety.

    The branch order is the specification's own, and the first match wins, so a
    fact that trips two rules takes the more severe one — which is what "only
    HIGH and CRITICAL refuse boot" requires, since a lower reading would let it
    through.
    """
    if fact.venue_read_missing:
        return "CRITICAL"
    if fact.quantity_mismatch or fact.status_mismatch:
        return "HIGH"
    if fact.kind is DriftKind.FUNDS and fact.funds_difference > FUNDS_DRIFT_THRESHOLD_INR:
        return "HIGH"
    return "LOW"


def refuses_boot(severity: str) -> bool:
    """Whether *severity* refuses boot. HIGH and CRITICAL do; LOW does not."""
    return severity in BOOT_REFUSING_SEVERITIES


def severity_totals(facts: Iterable[DriftFact]) -> dict[str, int]:
    """Count *facts* by derived severity, with every severity present at zero.

    A consumer renders a row per severity, so a missing key is a rendering
    bug waiting to happen; the totals always carry all three.
    """
    totals = dict.fromkeys(SEVERITIES, 0)
    for fact in facts:
        totals[derive_severity(fact)] += 1
    return totals


def facts_from_drift_item(item: DriftItem) -> DriftFact:
    """Read a :class:`DriftItem`'s *observations* into a :class:`DriftFact`.

    This is the one place ``DriftItem`` is touched, and it reads only the
    measurement fields — ``kind``, the quantities, and the reason text that
    names the comparison that failed. ``item.severity`` is never read, so
    feeding the engine's stored verdict in is not expressible.

    The mapping is a *translation*, not a guess at intent: the reason strings
    are the ``ReconciliationEngine``'s own vocabulary (``"quantity mismatch"``,
    ``"status lag"``, ``"missing broker order"``), and each one names a
    comparison whose failure is a fact. A reason this module does not recognise
    yields no measurement rather than an invented one, and then falls through
    to LOW — see :func:`derive_severity`, where LOW is the floor.
    """
    reason = (item.reason or "").lower()
    kind = DriftKind.ORDER if "order" in item.kind else (
        DriftKind.FUNDS if "funds" in item.kind else DriftKind.POSITION
    )

    # A "missing local/broker order" is a read that came back absent, so it is
    # the missing-venue-read fact, not a quantity disagreement.
    venue_missing = "missing" in reason

    quantity_mismatch = (
        "quantity mismatch" in reason
        or item.diff != Decimal("0")
    )
    status_mismatch = "status" in reason

    # Funds: the engine stores whole ``Account`` objects, so the difference is
    # measured here rather than carried on the item.
    funds_difference = _funds_difference(item) if kind is DriftKind.FUNDS else Decimal("0")

    return DriftFact(
        kind=kind,
        quantity_mismatch=quantity_mismatch,
        status_mismatch=status_mismatch,
        funds_difference=funds_difference,
        venue_read_missing=venue_missing,
    )


def _funds_difference(item: DriftItem) -> Decimal:
    """Absolute balance/equity difference between the two stored accounts."""
    local, remote = item.local, item.remote
    for attribute in ("balance", "equity"):
        left = getattr(local, attribute, None)
        right = getattr(remote, attribute, None)
        left_amount = getattr(left, "amount", None)
        right_amount = getattr(right, "amount", None)
        if left_amount is not None and right_amount is not None:
            return abs(Decimal(str(left_amount)) - Decimal(str(right_amount)))
    return Decimal("0")


def drift_facts(items: Sequence[DriftItem]) -> list[DriftFact]:
    """Translate drift items into facts, in order."""
    return [facts_from_drift_item(item) for item in items]


#: The three promotion answers AC-17 names. Note these are NOT the store's
#: vocabulary: ``ValidationReportStore.promotion_status`` returns ``passed`` /
#: ``not_promoted`` / ``not_evaluated``, and :func:`promotion_status` maps them.
class PromotionStatus(StrEnum):
    PROMOTABLE = "promotable"
    NOT_EVALUATED = "not_evaluated"
    NOT_PROMOTABLE = "not_promotable"


#: ``ValidationReportStore.promotion_status`` value -> the surface's answer.
#: ``passed`` becomes ``promotable`` because AC-17 asks what the *strategy's*
#: standing is, and a passing report means exactly that; the store's
#: ``not_promoted`` becomes ``not_promotable`` for the same reason. The two
#: negative answers are not merged: a strategy nobody evaluated is a different
#: operational fact from one that was evaluated and failed, and collapsing
#: them would report an un-run strategy as a rejected one.
_STORE_STATUS_MAP: dict[str, PromotionStatus] = {
    "passed": PromotionStatus.PROMOTABLE,
    "not_promoted": PromotionStatus.NOT_PROMOTABLE,
    "not_evaluated": PromotionStatus.NOT_EVALUATED,
}


def promotion_status(store_status: str) -> str:
    """Map a store promotion status onto the surface's three answers.

    Unrecognised values raise rather than defaulting: a fourth status in the
    store would change what "not promotable" means, and reading it as anything
    in particular is how a strategy gets a verdict it was never given.
    """
    mapped = _STORE_STATUS_MAP.get(store_status)
    if mapped is None:
        raise ValueError(f"unmapped promotion status: {store_status!r}")
    return mapped.value


@dataclass(frozen=True, slots=True)
class UnknownOrder:
    """One order the venue's answer never arrived for."""

    order_id: str
    instrument: str
    symbol: str
    side: str
    quantity: float
    correlation_id: str | None
    #: Seconds since the order was filed UNKNOWN. ``None`` when the event
    #: carried no timestamp, which is a gap in the record rather than an age
    #: of zero — an order of unknown age must not read as a fresh one.
    age_seconds: float | None
    since: str | None


@dataclass(frozen=True, slots=True)
class StrategySafety:
    """One registered strategy's lifecycle and promotion standing."""

    strategy_id: str
    version: str
    lifecycle_state: str
    promotion_status: str
    report_id: str | None
    promotion_failures: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        """The response row. ``failures`` is a list because JSON has no tuple."""
        return {
            "strategy_id": self.strategy_id,
            "version": self.version,
            "lifecycle_state": self.lifecycle_state,
            "promotion_status": self.promotion_status,
            "report_id": self.report_id,
            "promotion_failures": list(self.promotion_failures),
        }


@dataclass(frozen=True, slots=True)
class ReconciliationSafety:
    """Drift counted by derived severity, and when it was last looked for.

    ``last_checked_at`` is ``None`` when no reconciliation has ever run on this
    session. That is distinct from a run that found nothing, and
    :attr:`SafetyReport.reconciliation_checked` is the flag that tells them
    apart.
    """

    last_checked_at: str | None
    totals: dict[str, int]
    refusing: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "last_checked_at": self.last_checked_at,
            "totals_by_severity": dict(self.totals),
            "refusing_severities": sorted(BOOT_REFUSING_SEVERITIES),
            "refusing": self.refusing,
        }


@dataclass(frozen=True, slots=True)
class SafetyReport:
    """The whole AC-17 response, assembled once and serialised by the route."""

    feed: dict[str, Any]
    unknown_orders: tuple[UnknownOrder, ...]
    reconciliation: ReconciliationSafety
    strategies: tuple[StrategySafety, ...]
    #: True when a HIGH or CRITICAL discrepancy is outstanding. False for a
    #: LOW-only reconciliation, and false for one that has never run.
    boot_refused: bool
    #: True when a reconciliation has actually run on this session.
    reconciliation_checked: bool
    drift: list[DriftFact] = field(default_factory=list)

    def to_wire(self) -> dict[str, Any]:
        return {
            "feed": self.feed,
            "unknown_orders": [
                {
                    "order_id": o.order_id,
                    "instrument": o.instrument,
                    "symbol": o.symbol,
                    "side": o.side,
                    "quantity": o.quantity,
                    "correlation_id": o.correlation_id,
                    "age_seconds": o.age_seconds,
                    "since": o.since,
                }
                for o in self.unknown_orders
            ],
            "reconciliation": self.reconciliation.to_wire(),
            "strategies": [s.to_wire() for s in self.strategies],
            "boot_refused": self.boot_refused,
            "reconciliation_checked": self.reconciliation_checked,
        }


# ---------------------------------------------------------------------------
# Assembly from a session
# ---------------------------------------------------------------------------


def _age_seconds(since: datetime | None, now: datetime) -> float | None:
    if since is None:
        return None
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return max(0.0, (now - since).total_seconds())


def feed_state(session: Any) -> dict[str, Any]:
    """Feed health and the age of the last event.

    Delegates to the monitor that already owns the ``feed_status`` snapshot, so
    the safety surface and the websocket cannot disagree about how old the feed
    is. A session with no monitor reports the monitor's own null shape — absent
    is not healthy.
    """
    monitor = getattr(session, "feed_monitor", None)
    status = getattr(monitor, "status", None)
    if not callable(status):
        return {
            "state": None,
            "ready": None,
            "generation": None,
            "last_event_at": None,
            "age_seconds": None,
        }
    return dict(status())


def unresolved_unknown_orders(
    orders: Sequence[Any], event_times: dict[str, datetime], now: datetime,
) -> tuple[UnknownOrder, ...]:
    """The orders still sitting in ``UNKNOWN``, with age and correlation id.

    ``event_times`` maps order id -> when it was filed UNKNOWN, taken from the
    event log: :class:`~tradex_domain.execution.Order` carries no timestamp of
    its own, so the age has to come from the event that recorded the
    transition. An order with no entry there reports ``age_seconds=None``
    rather than zero.
    """
    out: list[UnknownOrder] = []
    for order in orders:
        status = getattr(order, "status", None)
        if str(getattr(status, "value", status)) != "UNKNOWN":
            continue
        order_id = str(getattr(order.order_id, "value", order.order_id))
        since = event_times.get(order_id)
        instrument = getattr(order, "instrument", None)
        correlation = getattr(order, "correlation_id", None)
        out.append(
            UnknownOrder(
                order_id=order_id,
                instrument=str(getattr(instrument, "instrument_id", "") or ""),
                symbol=str(getattr(instrument, "symbol", "") or ""),
                side=str(getattr(getattr(order, "side", None), "value", "") or ""),
                quantity=float(getattr(getattr(order, "quantity", None), "value", 0) or 0),
                correlation_id=(
                    str(getattr(correlation, "value", correlation)) if correlation else None
                ),
                age_seconds=_age_seconds(since, now),
                since=since.isoformat() if since is not None else None,
            ),
        )
    out.sort(key=lambda o: o.order_id)
    return tuple(out)


def _unknown_transition_times(events: Iterable[Any]) -> dict[str, datetime]:
    """When each order was filed UNKNOWN, from the event log.

    Reads ``OrderPlaced`` carriers: the engine appends one with the UNKNOWN
    order when the submission boundary was crossed, and it is the only place
    the transition is timestamped. A later carrier for the same order in a
    different status does not overwrite the entry — the question is when it
    became unresolved, not when it was last touched.
    """
    times: dict[str, datetime] = {}
    for event in events:
        order = getattr(event, "order", None)
        if order is None:
            continue
        status = str(getattr(getattr(order, "status", None), "value", ""))
        if status != "UNKNOWN":
            continue
        order_id = str(getattr(order.order_id, "value", order.order_id))
        times.setdefault(order_id, event.timestamp)
    return times


def _lifecycle_value(runtime: Any) -> str:
    """A runtime's lifecycle state as the wire string.

    ``StrategyRuntime.lifecycle`` is a property returning a
    ``StrategyLifecycle`` StrEnum, so ``str(enum)`` would serve the member
    NAME. Its ``value`` is the lowercase wire form the registry and the
    websocket already use.
    """
    lifecycle = getattr(runtime, "lifecycle", None)
    return str(getattr(lifecycle, "value", lifecycle) or "")


def _strategy_rows(session: Any) -> tuple[StrategySafety, ...]:
    """Each registered strategy's lifecycle and promotion standing.

    Promotion is read through ``ValidationReportStore.promotion_status``, which
    rehydrates the report and re-applies the promotion bar to the stored
    metrics — so a verdict column someone edited in the database cannot make an
    unvalidated strategy look promotable.
    """
    registry = getattr(session, "strategy_registry", None)
    runtimes = list(getattr(registry, "list_all", list)()) if registry is not None else []
    if not runtimes:
        return ()

    store = getattr(session, "validation_report_store", None)
    rows: list[StrategySafety] = []
    for runtime in runtimes:
        artifact = getattr(runtime, "artifact", None)
        strategy_id = str(getattr(artifact, "strategy_id", "") or "")
        report = store.latest(strategy_id) if store is not None else None
        rows.append(
            StrategySafety(
                strategy_id=strategy_id,
                version=str(getattr(artifact, "version", "") or ""),
                lifecycle_state=_lifecycle_value(runtime),
                promotion_status=(
                    promotion_status(store.promotion_status(strategy_id))
                    if store is not None
                    else PromotionStatus.NOT_EVALUATED.value
                ),
                report_id=str(getattr(report, "report_id", "") or "") or None,
                promotion_failures=tuple(getattr(report, "failures", ()) or ()),
            ),
        )
    return tuple(rows)


def build_safety_report(
    session: Any,
    *,
    now: datetime | None = None,
) -> SafetyReport:
    """Assemble the whole AC-17 response from a live session.

    Every input is read defensively: the safety surface is the thing an
    operator reaches for when something is already wrong, so a component that
    is absent reports itself absent rather than raising and taking the one
    endpoint that explains the outage down with it.
    """
    now = now or datetime.now(UTC)

    engine = getattr(session, "engine", None)
    cache = getattr(engine, "cache", None)
    orders = list(getattr(cache, "all_orders", list)()) if cache is not None else []

    event_store = getattr(session, "event_store", None)
    events = list(event_store.replay("orders")) if event_store is not None else []

    unknown = unresolved_unknown_orders(
        orders, _unknown_transition_times(events), now,
    )

    last_checked, facts = _reconciliation(session, events)
    totals = severity_totals(facts)
    refusing = sum(count for sev, count in totals.items() if refuses_boot(sev))

    return SafetyReport(
        feed=feed_state(session),
        unknown_orders=unknown,
        reconciliation=ReconciliationSafety(
            last_checked_at=last_checked,
            totals=totals,
            refusing=refusing,
        ),
        strategies=_strategy_rows(session),
        boot_refused=refusing > 0,
        reconciliation_checked=last_checked is not None,
        drift=facts,
    )


def _reconciliation(session: Any, events: Sequence[Any]) -> tuple[str | None, list[DriftFact]]:
    """``(last_checked_at, facts)`` for this session's reconciliation.

    The drift items themselves come from the last reconcile run when the
    session kept them; otherwise the *current* comparison is re-derived from
    the engine's local book against the venue's, so a stale summary is never
    served as if it were fresh. Facts are re-derived here from the items'
    observations, never from their stored severities.
    """
    from tradex_domain.events import ReconciliationResult

    checks = [e for e in events if isinstance(e, ReconciliationResult)]
    last_checked = checks[-1].timestamp.isoformat() if checks else None

    drifts = getattr(session, "last_reconciliation", None)
    if drifts is None:
        drifts = _rerun_reconciliation(session)
    return last_checked, drift_facts(list(drifts or ()))


def _rerun_reconciliation(session: Any) -> list[DriftItem]:
    """Compare the local book against the venue, degrading to no drift.

    Reconciliation is side-effect free by contract, so re-running it for a read
    is safe. A venue read that raises yields no items rather than an exception:
    "we could not check" must not be rendered as "we checked and found
    nothing", and :attr:`SafetyReport.reconciliation_checked` is what carries
    that distinction.
    """
    engine = getattr(session, "engine", None)
    reconcile = getattr(engine, "reconcile", None)
    if not callable(reconcile):
        return []
    try:
        positions = engine.cache.all_positions()
        venue_positions = session.broker.get_positions()
        return list(reconcile(broker_positions=venue_positions, local_positions=positions))
    except Exception:  # noqa: BLE001 - the safety surface must still answer
        return []


__all__ = [
    "BOOT_REFUSING_SEVERITIES",
    "FUNDS_DRIFT_THRESHOLD_INR",
    "SEVERITIES",
    "DriftFact",
    "DriftKind",
    "PromotionStatus",
    "ReconciliationSafety",
    "SafetyReport",
    "StrategySafety",
    "UnknownOrder",
    "build_safety_report",
    "derive_severity",
    "drift_facts",
    "facts_from_drift_item",
    "feed_state",
    "promotion_status",
    "refuses_boot",
    "severity_totals",
    "unresolved_unknown_orders",
]
