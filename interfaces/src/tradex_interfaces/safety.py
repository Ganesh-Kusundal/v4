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

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from logging import getLogger
from typing import Any

from tradex_execution.reconciliation import DriftItem

log = getLogger(__name__)

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


def _registry_runtimes(registry: Any) -> list[Any]:
    """The runtimes a registry holds, whether it is the registry or a getter.

    ``TradingSession`` exposes the registry as ``strategy_registry()`` — a
    zero-arg *method* [REF-5], not a property — so ``getattr(session,
    "strategy_registry")`` yields a bound method. Reading ``.list_all`` off
    that method fails, the old ``getattr(..., list)`` fallback quietly
    substituted the builtin ``list``, and ``list()`` is empty: a booted
    session with three registered strategies reported **zero**, and reported it
    as "no strategies exist" rather than as an error. That is the null-degrade
    this surface must not have.

    Both shapes are therefore resolved explicitly, and a registry that is
    neither yields nothing rather than a fabricated list.
    """
    if registry is None:
        return []
    candidate = registry() if callable(registry) else registry
    list_all = getattr(candidate, "list_all", None)
    if not callable(list_all):
        return []
    return list(list_all())


def _strategy_rows(session: Any) -> tuple[StrategySafety, ...]:
    """Each registered strategy's lifecycle and promotion standing.

    Promotion is read through ``ValidationReportStore.promotion_status``, which
    rehydrates the report and re-applies the promotion bar to the stored
    metrics — so a verdict column someone edited in the database cannot make an
    unvalidated strategy look promotable.
    """
    registry = getattr(session, "strategy_registry", None)
    runtimes = list(_registry_runtimes(registry))
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
    order_events, system_events = _replay_for_safety(event_store)

    unknown = unresolved_unknown_orders(
        orders, _unknown_transition_times(order_events), now,
    )

    # The last-checked marker is a ReconciliationResult, which the event store
    # routes to the "system" stream, not "orders" (see
    # sqlite_event_store._ORDER_EVENT_CLASSNAMES, which deliberately excludes
    # it). Reading only "orders" therefore made last_checked_at structurally
    # unreachable: even with a fully wired store it was a permanent None, and
    # the route reported "never checked" about a session that had just
    # reconciled. Both streams are read, each for its own question.
    last_checked, facts = _reconciliation(session, system_events)
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


def _replay_for_safety(event_store: Any) -> tuple[list[Any], list[Any]]:
    """``(order_events, system_events)`` from the durable log.

    Two streams, two questions. Order lifecycle carriers (``OrderPlaced`` and
    friends) live in ``orders`` and date the UNKNOWN orders. The
    ``ReconciliationResult`` marker lives in ``system`` — the event store
    routes it there deliberately — and dates the last reconciliation. Reading
    ``orders`` alone made the second question unanswerable, which is why
    ``last_checked_at`` was a permanent ``None``.

    A stream the store does not carry, or a store that cannot replay, yields an
    empty list for that stream rather than taking the route down: the surface
    reports "absent" for the part it could not read.
    """
    if event_store is None:
        return [], []
    streams: list[list[Any]] = []
    for stream in ("orders", "system"):
        try:
            streams.append(list(event_store.replay(stream)))
        except Exception:  # noqa: BLE001 — the safety surface must still answer
            streams.append([])
    return streams[0], streams[1]


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


@dataclass(frozen=True, slots=True)
class VenueAdoption:
    """The local book after the venue's positions were *adopted*, not merely read.

    Lane 5e: reconciliation used to report drift and stop there. A strategy that
    filled at the venue while the local book lagged stayed wrong forever, because
    nothing ever wrote the venue's answer into the book. This is the record of one
    adoption: the positions written, the ones untouched, and the reasons nothing
    was written where nothing was.

    The record is *complete*, not best-effort: :attr:`positions` plus
    :attr:`retained` plus :attr:`cleared` plus :attr:`skipped` accounts for every
    instrument on either side of the comparison, so a caller can prove nothing was
    silently dropped. A gap in that accounting is the exact corruption this object
    exists to make impossible.

    The venue's figures are carried on the adopted :class:`Position` objects as
    plain domain values; the local figures they replaced are in :attr:`previous`
    so a caller (and a log line) can say what changed, not just that it did.
    """

    #: Adopted venue positions, keyed by instrument id, written to the book.
    positions: dict[str, Any] = field(default_factory=dict)
    #: Local positions the venue also reported, whose figures already agreed.
    #: Untouched deliberately: an adoption is a correction, not a rewrite.
    retained: dict[str, Any] = field(default_factory=dict)
    #: Local positions the venue's complete read reported as flat. Written as a
    #: zero-quantity position rather than deleted, because the book is keyed by
    #: instrument and a strategy that next reads the position must find one.
    cleared: dict[str, Any] = field(default_factory=dict)
    #: Instrument ids the adoption did NOT write, with the reason. The reason is
    #: a machine-readable short string, not a free sentence: it is counted in
    #: ``venue_adoption_skipped_total{reason=...}`` and a sentence there would be
    #: an unbounded label set.
    skipped: dict[str, str] = field(default_factory=dict)
    #: The local positions that were replaced, keyed by instrument id, so the log
    #: and the caller can state what changed rather than merely that it did.
    previous: dict[str, Any] = field(default_factory=dict)

    @property
    def adopted_count(self) -> int:
        """Instruments whose position the adoption actually wrote."""
        return len(self.positions)

    @property
    def is_empty(self) -> bool:
        """An adoption that changed nothing, including one that was refused."""
        return not (
            self.positions or self.cleared or self.skipped
        )

    def as_log_rows(self) -> list[dict[str, Any]]:
        """One machine-readable row per instrument the adoption acted on.

        Only the instruments that *changed* appear: :attr:`retained` is the
        already-agreeing majority and logging it at INFO on every adoption would
        drown the signal in noise.
        """
        rows: list[dict[str, Any]] = []
        for iid, position in self.positions.items():
            previous = self.previous.get(iid)
            rows.append(
                {
                    "instrument_id": iid,
                    "action": "adopted",
                    "previous_quantity": _quantity_of(previous),
                    "venue_quantity": _quantity_of(position),
                    "venue_avg_price": _avg_price_of(position),
                    "strategy_id": _strategy_id_of(previous),
                }
            )
        for iid, position in self.cleared.items():
            previous = self.previous.get(iid)
            rows.append(
                {
                    "instrument_id": iid,
                    "action": "cleared",
                    "previous_quantity": _quantity_of(previous),
                    "venue_quantity": _quantity_of(position),
                    "venue_avg_price": _avg_price_of(position),
                    "strategy_id": _strategy_id_of(previous),
                }
            )
        for iid, reason in self.skipped.items():
            rows.append(
                {
                    "instrument_id": iid,
                    "action": "skipped",
                    "reason": reason,
                    "previous_quantity": _quantity_of(self.previous.get(iid)),
                }
            )
        return rows


def _attr(position: Any, name: str) -> Any:
    """A field of *position*, attribute or mapping key, or ``None" when unreadable.

    A broker adapter that hands back a row or a mapping instead of a
    :class:`Position` must not break the comparison before it starts, and
    ``getattr`` on a mapping *raises* rather than returning a default. ``None`` is
    the signal for "absent", never an exception: the read's contract is that it
    never raises, and a caller treats ``None`` as unadoptable rather than guessing.
    """
    if isinstance(position, Mapping):
        return position.get(name)
    try:
        return getattr(position, name, None)
    except Exception:  # noqa: BLE001 - an exotic object, not a position
        return None


def _quantity_of(position: Any) -> Any:
    """A position's quantity as a plain Decimal, or ``None`` when absent."""
    if position is None:
        return None
    quantity = _attr(position, "quantity")
    return getattr(quantity, "value", quantity)


def _avg_price_of(position: Any) -> Any:
    """A position's average price as a plain Decimal, or ``None`` when absent."""
    if position is None:
        return None
    price = _attr(position, "avg_price")
    return getattr(price, "value", price)


def _strategy_id_of(position: Any) -> str | None:
    """The strategy that opened *position*, or ``None`` when it is unknown."""
    if position is None:
        return None
    return _attr(position, "strategy_id")


def _position_instrument_id(position: Any) -> str | None:
    """The instrument id a position is keyed by, or ``None`` when unreadable.

    The venue's objects are not ours: a broker adapter that hands back a mapping or
    a row instead of a :class:`Position` must not break the comparison before it
    starts. ``None`` is the signal for "not a position" rather than an exception,
    and the caller treats it as unadoptable rather than inventing a key.
    """
    if position is None:
        return None
    instrument = _attr(position, "instrument")
    if instrument is not None:
        return str(getattr(instrument, "instrument_id", instrument))
    key = _attr(position, "instrument_id")
    if key is None:
        return None
    return str(key)


def _venue_quantity(position: Any) -> Decimal | None:
    """The venue's quantity for *position* as a Decimal, or ``None`` when absent."""
    quantity = _attr(position, "quantity")
    value = getattr(quantity, "value", quantity)
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None


#: The ways an instrument can be skipped, named once so the counter labels and the
#: tests agree. Every one is a refusal to adopt, and each is a different reason an
#: operator needs to see before clearing drift manually.
SKIP_UNREADABLE_POSITION = "unreadable_position"
SKIP_UNREADABLE_QUANTITY = "unreadable_quantity"
SKIP_UNREADABLE_INSTRUMENT = "unreadable_instrument"
SKIP_UNSETTLED_FILL = "unsettled_fill"
#: The placeholder for a venue row whose instrument could not be read. A row that
#: cannot be keyed cannot be counted by name, but it *can* be counted, and a
#: partial read that reports zero unreadable instruments would read as complete.
_UNREADABLE_INSTRUMENT = "<unreadable:instrument>"
#: The venue's complete read named no row for an instrument the local book holds.
#: That *is* the venue saying flat — but only if the read really was complete, and
#: proving it is the caller's job (``known_instruments`` in
#: :func:`read_venue_positions`). A caller that cannot prove completeness gets the
#: skip rather than the clear, because clearing is the destructive direction and a
#: gap reading as "flat" is the corruption lane 5e is written against.
SKIP_VENUE_OMITTED = "venue_omitted"

#: Order statuses whose fills the venue's snapshot cannot yet have seen.
_UNSETTLED_ORDER_STATUSES: frozenset[str] = frozenset(
    {
        "NEW",
        "PENDING",
        "ACK",
        "SUBMITTED",
        "PARTIALLY_FILLED",
        "UNKNOWN",
    }
)


def unsettled_local_instruments(orders: Sequence[Any]) -> frozenset[str]:
    """Instrument ids the local book believes are mid-flight, so must not be adopted over.

    The local book's own in-flight work is *newer* than the venue's snapshot in the
    one way that matters: a fill that crossed the venue boundary and is not yet on
    the venue's ``get_positions`` answer is destroyed by an adoption that treats the
    venue's figure as authoritative. The venue lags, and the local book is right.
    Adopting over it would not correct the book, it would corrupt it — and silently:
    the adoption logs a "correction" while making the book wrong, which is the exact
    failure mode lane 5e names.

    The check is deliberately conservative and deliberately narrow:

    * ``UNKNOWN`` — an order the venue may have accepted and not yet reported. Its
      fill is provably absent from the venue answer, so the instrument is protected
      for as long as the uncertainty stands.
    * ``NEW``/``PENDING``/``ACK``/``SUBMITTED``/``PARTIALLY_FILLED`` — may complete
      at any instant, and the venue answer is a snapshot from before it did. A
      partially filled order's *resting* leg can still fill after the snapshot, so
      the venue's quantity for it is already stale.

    It is narrow on purpose. It does not protect every instrument that ever carried
    an order, because that would refuse to adopt at all, and refusing to adopt is
    the status quo lane 5e exists to end. The protection covers the instruments the
    local book actively believes unresolved, and lifts the moment they resolve.
    """
    unsettled: set[str] = set()
    for order in orders or ():
        status = getattr(order, "status", None)
        value = str(getattr(status, "value", status) or "")
        if value in _UNSETTLED_ORDER_STATUSES:
            iid = _position_instrument_id(getattr(order, "instrument", None))
            if iid is not None:
                unsettled.add(iid)
    return frozenset(unsettled)


#: The adoption corrects *figures*, so a difference below this is not a disagreement
#: worth writing: it is rounding in the venue's own quantization. Keeping the book
#: untouched for those keeps the adoption log to the corrections an operator needs
#: to see, and is what makes a re-derivation after a successful adoption report no
#: change (idempotency) rather than a permanent paise-level churn.
_ADOPTION_TOLERANCE: Decimal = Decimal("0.01")


def derive_venue_adoption(
    *,
    local_positions: Sequence[Any],
    venue_positions: Sequence[Any],
    unsettled_instruments: frozenset[str] | set[str] | None = None,
) -> VenueAdoption:
    """Decide what an adoption of *venue_positions* would write to the local book.

    Pure. No book is touched, so a caller can inspect the plan, log it, and only
    then apply it — and a caller that decides not to apply it has lost nothing. The
    plan's four buckets account for every instrument on either side, so nothing is
    silently dropped (see :class:`VenueAdoption`).

    Completeness of the venue answer is the caller's to prove (see
    :func:`read_venue_positions`), not this function's to assume: an instrument the
    local book holds that the venue omits is a *skip*, never a zero, because a gap
    read as flat is the corruption this module exists to prevent. Only the caller
    that proved the read complete gets a clear.
    """
    local_by_id: dict[str, Any] = {}
    for position in local_positions or ():
        iid = _position_instrument_id(position)
        if iid is not None:
            local_by_id[iid] = position

    venue_by_id: dict[str, Any] = {}
    unreadable: set[str] = set()
    for position in venue_positions or ():
        iid = _position_instrument_id(position)
        if iid is None:
            # A venue row that cannot even be keyed is unadoptable, and adopting
            # "everything we could read" would quietly drop it.
            unreadable.add(_UNREADABLE_INSTRUMENT)
            continue
        venue_by_id[iid] = position

    unsettled = frozenset(unsettled_instruments or ())

    positions: dict[str, Any] = {}
    retained: dict[str, Any] = {}
    cleared: dict[str, Any] = {}
    skipped: dict[str, str] = {}
    previous: dict[str, Any] = {}

    # Every instrument the local book holds, whether or not the venue named it.
    for iid in sorted(local_by_id):
        local = local_by_id[iid]
        venue = venue_by_id.get(iid)
        if venue is None:
            # The venue named no row for an instrument the book holds. That is the
            # venue saying flat only if the read was proven complete (see
            # :func:`read_venue_positions`); a caller that did not prove it gets the
            # skip rather than the clear, because clearing is the destructive
            # direction and a gap reading as flat is the corruption this module
            # exists to prevent.
            skipped[iid] = SKIP_VENUE_OMITTED
            continue
        venue_quantity = _venue_quantity(venue)
        local_quantity = _venue_quantity(local)
        if venue_quantity is None or local_quantity is None:
            # A quantity this side cannot read is an unreadable reading, and the
            # honest report is "could not adopt" rather than a guess at zero.
            skipped[iid] = SKIP_UNREADABLE_QUANTITY
            continue
        if venue_quantity == local_quantity and _agrees_on_price(local, venue):
            # Already agrees. An adoption is a correction, not a rewrite: touching
            # an agreeing position would churn the log on every poll forever.
            retained[iid] = local
            continue
        if iid in unsettled:
            # Disagreeing *and* unsettled is the worst case: the book has a fill the
            # venue has not reflected, so the venue's figure is wrong for the very
            # quantity it reports. Adopting would undo the fill.
            skipped[iid] = SKIP_UNSETTLED_FILL
            continue
        if venue_quantity == Decimal("0"):
            # The venue reports flat. A zero-quantity position is written rather than
            # a delete, because the cache is keyed by instrument and a strategy that
            # reads the position next must find one.
            cleared[iid] = _flat_position(local) or _flat_position(venue)
            previous[iid] = local
            continue
        adopted = _adopted_position(local, venue)
        if adopted is None:
            skipped[iid] = SKIP_UNREADABLE_POSITION
            continue
        positions[iid] = adopted
        previous[iid] = local

    # Instruments only the venue named: the local book is flat and the venue says
    # there is a position. These are exactly what reconciliation used to report as
    # drift and then leave in place; adoption writes them.
    for iid in sorted(set(venue_by_id) - set(local_by_id)):
        venue = venue_by_id[iid]
        venue_quantity = _venue_quantity(venue)
        if venue_quantity is None:
            skipped[iid] = SKIP_UNREADABLE_QUANTITY
            continue
        if venue_quantity == Decimal("0"):
            # Flat both sides: nothing to write, and an entry here would only churn
            # the skip counter on every poll.
            continue
        adopted = _adopted_position(None, venue)
        if adopted is None:
            skipped[iid] = SKIP_UNREADABLE_POSITION
            continue
        positions[iid] = adopted

    for iid in unreadable:
        skipped[iid] = SKIP_UNREADABLE_INSTRUMENT

    return VenueAdoption(
        positions=positions,
        retained=retained,
        cleared=cleared,
        skipped=skipped,
        previous=previous,
    )


def _agrees_on_price(local: Any, venue: Any) -> bool:
    """Whether two agreeing-quantity positions also agree on average price."""
    local_price = _avg_price_of(local)
    venue_price = _avg_price_of(venue)
    if local_price is None or venue_price is None:
        return True
    return abs(Decimal(str(local_price)) - Decimal(str(venue_price))) <= _ADOPTION_TOLERANCE


def _flat_position(reference: Any) -> Any:
    """A flat position on *reference*'s instrument, carrying its attribution.

    Constructed from the domain value objects so the cleared row is a real
    :class:`Position` the cache can store, not a hand-rolled mapping.
    """
    from tradex_domain.execution import Position
    from tradex_domain.value_objects import Money, Price, Quantity

    instrument = _attr(reference, "instrument")
    if instrument is None:
        return None
    currency = getattr(instrument, "currency", "INR")
    return Position(
        instrument=instrument,
        quantity=Quantity(value=Decimal("0")),
        avg_price=Price(value=Decimal("0")),
        realized_pnl=Money(amount=Decimal("0"), currency=currency),
        unrealized_pnl=Money(amount=Decimal("0"), currency=currency),
        mark_price=None,
        marked_at=None,
        mark_source=None,
        strategy_id=getattr(reference, "strategy_id", None),
    )


def _cleared_position(local: Any, venue: Any) -> Any:
    """The flat position written when the venue reports an instrument the book held."""
    cleared = _flat_position(local)
    if cleared is not None:
        return cleared
    # The local position carried no instrument; fall back to the venue's.
    return _flat_position(venue)


def _adopted_position(local: Any, venue: Any) -> Any:
    """The position the adoption writes: the venue's figures on the venue's object.

    The venue's :class:`Position` is the authoritative figure by construction — it
    is the object ``broker.get_positions()`` returned, with the venue's own
    instrument, average price and currency. Copying its fields onto a new object
    would only risk quantizing or dropping one; the adopted position *is* the
    venue's position.

    What is not copied is :attr:`Position.mark_price`: a mark is a *quote* fact,
    refreshed by the quote-driven marking service, and the venue's position answer
    carries either no mark or a stale one that would then be believed fresh until
    the next quote. The mark is dropped so the next quote re-marks the position.
    """
    if venue is None:
        return None
    instrument = _attr(venue, "instrument")
    if instrument is None:
        return None
    currency = getattr(instrument, "currency", "INR")
    try:
        from tradex_domain.execution import Position
        from tradex_domain.value_objects import Money, Price, Quantity

        return Position(
            instrument=instrument,
            quantity=Quantity(value=_venue_quantity(venue) or Decimal("0")),
            avg_price=Price(value=_venue_avg_price_value(venue, currency)),
            realized_pnl=_venue_realized(venue, currency),
            unrealized_pnl=Money(amount=Decimal("0"), currency=currency),
            mark_price=None,
            marked_at=None,
            mark_source=None,
            # The venue cannot attribute a position to a strategy: it does not know
            # the strategy id. The local book's attribution is preserved when it
            # exists, so strategy P&L books do not silently lose a position.
            strategy_id=(getattr(local, "strategy_id", None) if local is not None else None),
        )
    except Exception:  # noqa: BLE001 - a venue object that is not a Position
        return None


def _venue_avg_price_value(venue: Any, currency: str) -> Decimal:
    """The venue's average price as a Decimal, ``0`` when the venue omits it."""
    price = _avg_price_of(venue)
    if price is None:
        return Decimal("0")
    try:
        return Decimal(str(price))
    except (ArithmeticError, TypeError, ValueError):
        return Decimal("0")


def _venue_realized(venue: Any, currency: str) -> Any:
    """The venue's realized P&L as ``Money``, zero when the venue omits it."""
    realized = _attr(venue, "realized_pnl")
    amount = getattr(realized, "amount", None)
    if amount is None:
        amount = Decimal("0")
    try:
        from tradex_domain.value_objects import Money

        return Money(amount=Decimal(str(amount)), currency=currency)
    except (ArithmeticError, TypeError, ValueError):
        from tradex_domain.value_objects import Money

        return Money(amount=Decimal("0"), currency=currency)


@dataclass(frozen=True, slots=True)
class VenueRead:
    """A venue positions read, sealed: either complete or failed, never partial.

    This is the type that makes "a failed read does not cause adoption" a
    structural property rather than a rule to remember. ``get_positions()`` has
    three outcomes — a full list, a partial list, or an exception — and only the
    first is usable:

    * **Failure** (:attr:`failed` True) carries no positions at all. The read
      produced nothing usable, so adopting from it is not expressible.
    * **Partial** (:attr:`partial` True) returned *some* positions and the venue
      also named instruments it could not read. Adopting a partial answer would
      treat the unread instruments as flat, which is the corruption lane 5e is
      written against, so the read is refused and the instruments are reported.
    * **Complete** is the only state an adoption may draw on.

    The builder (:func:`read_venue_positions`) is the only constructor callers use,
    so a caller cannot assemble a "complete" read from a failed one.
    """

    positions: tuple[Any, ...] = ()
    #: Instrument ids the venue reported as unreadable. Empty for a clean read.
    unreadable_instruments: tuple[str, ...] = ()
    failed: bool = False
    #: What failed, when something did. A short machine-readable string.
    failure: str = ""
    #: The exception the venue raised, for a log's ``exc_info``. Never surfaced.
    error: BaseException | None = None
    #: When the read completed, or ``None`` if it never did. An adoption refuses a
    #: read whose stamp is older than the caller's own last successful read.
    read_at: datetime | None = None

    @property
    def ok(self) -> bool:
        """The read is usable: it did not fail and it is not partial."""
        return not self.failed and not self.partial

    @property
    def partial(self) -> bool:
        """The venue returned some positions and could not read others."""
        return bool(self.unreadable_instruments) and not self.failed

    @property
    def refused(self) -> str:
        """The reason an adoption would refuse this read, or ``""`` when usable."""
        if self.failed:
            return self.failure or "venue_read_failed"
        if self.partial:
            return "venue_read_partial"
        return ""

    def as_log_dict(self) -> dict[str, Any]:
        """The read's verdict for a log line, with no exception object in it."""
        return {
            "ok": self.ok,
            "failed": self.failed,
            "partial": self.partial,
            "failure": self.failure or None,
            "positions": len(self.positions),
            "unreadable_instruments": sorted(self.unreadable_instruments),
            "refused": self.refused or None,
        }


def read_venue_positions(
    broker: Any,
    *,
    known_instruments: Sequence[str] | None = None,
    now: datetime | None = None,
) -> VenueRead:
    """Read the venue's positions once, sealing the outcome as complete or failed.

    Never raises. The three outcomes a venue read can have are the three states of
    :class:`VenueRead`, and an exception is converted to a *failed* read rather
    than propagated: a monitor that dies on a venue error is a monitor that stopped
    reconciling, which is the opposite of the safe direction.

    **Partial reads.** A venue adapter that returns positions for some instruments
    and reports others unreadable has handed back a partial book. This is detected
    two ways and refused either way:

    * the answer contains positions whose instrument is unreadable (counted in
      :attr:`VenueRead.unreadable_instruments`), or
    * the caller named ``known_instruments`` — the instruments the book holds — and
      the venue's answer names a strict subset. ``known_instruments`` is what makes
      the second detection possible: without it a venue that silently dropped half
      the book would read as complete, and an adoption would flatten the half it
      dropped.
    """

    try:
        positions = list(broker.get_positions())
    except Exception as exc:  # noqa: BLE001 - monitoring must not crash
        return VenueRead(
            failed=True,
            failure="venue_get_positions_failed",
            error=exc,
        )
    stamp = now or datetime.now(UTC)

    unreadable = [
        iid
        for iid in (_position_instrument_id(p) for p in positions)
        if iid is None
    ]
    if unreadable:
        # An unreadable row inside a *successful* call is the partial case: the
        # venue answered with gaps rather than raising.
        return VenueRead(
            positions=tuple(positions),
            unreadable_instruments=tuple([_UNREADABLE_INSTRUMENT] * len(unreadable)),
            read_at=stamp,
        )
    if known_instruments is not None:
        seen = {_position_instrument_id(p) for p in positions}
        missing = sorted(
            iid for iid in set(known_instruments) if iid not in seen
        )
        if missing:
            # The book holds instruments the venue's answer does not name. The
            # read is partial and must not be adopted as if the missing ones were
            # flat.
            return VenueRead(
                positions=tuple(positions),
                unreadable_instruments=tuple(missing),
                read_at=stamp,
            )
    return VenueRead(positions=tuple(positions), read_at=stamp)


@dataclass(frozen=True, slots=True)
class AdoptionOutcome:
    """The result of one adoption attempt: the plan, and whether it was applied.

    An adoption is refused far more often than it is applied, and the refusal is
    the load-bearing behaviour, so the outcome carries the *reason* the read was
    refused (:attr:`refusal`) alongside the plan an operator can inspect.
    """

    adoption: VenueAdoption
    #: ``True`` when the plan was written to the book. ``False`` is a refusal.
    applied: bool
    #: Why a refused adoption was refused. Empty when applied.
    refusal: str = ""
    #: The venue read the adoption was derived from, for the log.
    read: VenueRead | None = None

    @property
    def adopted_count(self) -> int:
        return self.adoption.adopted_count


def adopt_venue_positions(
    *,
    broker: Any,
    local_positions: Sequence[Any],
    cache: Any,
    unsettled_instruments: frozenset[str] | set[str] | None = None,
    known_instruments: Sequence[str] | None = None,
    last_read_at: datetime | None = None,
    now: datetime | None = None,
    adopt: bool = True,
) -> AdoptionOutcome:
    """Adopt the venue's positions into the book, or refuse with a reason.

    This is lane 5e's mutation. It is:

    * **Explicit** — a caller has to ask; nothing reconciles into the book on its
      own. ``adopt=False`` derives the plan and applies nothing, so the safety
      surface and a dry-run can show an operator what *would* change.
    * **Conditioned on a complete venue read** — :func:`read_venue_positions`
      refuses a failed or partial read before any position is touched. This is the
      structural guarantee that a bad read cannot corrupt a good book.
    * **Idempotent** — the plan's :attr:`VenueAdoption.retained` set is the
      already-agreeing majority, and re-deriving after a completed adoption
      produces an empty plan. Running it twice writes the same positions twice and
      no more, so a retry after a partial failure converges rather than compounds.
    * **Logged with what it changed** — every instrument the plan acted on is in
      :meth:`VenueAdoption.as_log_rows`, and the refusal is logged at WARNING
      rather than swallowed.

    The stale-read case is the last guard, and it is what makes a *retried* read
    safe: ``last_read_at`` is the caller's previous successful read, and a read
    that arrives *older* than it is refused, because a retry that lands out of
    order would overwrite a fresher book with a staler one.
    """
    read = read_venue_positions(
        broker, known_instruments=known_instruments, now=now,
    )
    refusal = read.refused
    if refusal == "" and last_read_at is not None and read.read_at is not None:
        if read.read_at < last_read_at:
            # A read older than the one already applied is a retry of a stale
            # snapshot. Adopting it would move the book backwards.
            refusal = "venue_read_stale"
    if refusal:
        log.warning(
            "venue position adoption refused: %s (read=%s)",
            refusal, read.as_log_dict(),
        )
        return AdoptionOutcome(
            adoption=VenueAdoption(),
            applied=False,
            refusal=refusal,
            read=read,
        )
    plan = derive_venue_adoption(
        local_positions=local_positions,
        venue_positions=read.positions,
        unsettled_instruments=unsettled_instruments,
    )
    if not adopt:
        return AdoptionOutcome(adoption=plan, applied=False, read=read)
    _apply_venue_adoption(plan, cache)
    _log_venue_adoption(plan, read)
    return AdoptionOutcome(adoption=plan, applied=True, read=read)


def _apply_venue_adoption(plan: VenueAdoption, cache: Any) -> None:
    """Write the plan's positions into *cache*, or nothing if the plan is empty.

    The write is the cache's own method, so the book's locking and its key
    scheme stay where they are owned — and when the cache offers the batch
    seam (``set_positions``), the whole plan lands under one set of instrument
    locks. That is the plan's own semantics: the rows are correct *together*,
    and a per-row apply lets a fill's read-modify-write slip between two rows
    or a reader see a half-applied plan (P0-F6).

    A cache with no batch seam still works, but the fallback is per-row and
    therefore NOT atomic: it is logged as such, because an adoption that
    silently half-applied while reporting a full correction is exactly the
    corruption this module exists to prevent. A cache with no write method at
    all is reported rather than worked around.
    """
    if plan.is_empty:
        return
    rows = [*plan.positions.values(), *plan.cleared.values()]
    write_batch = getattr(cache, "set_positions", None)
    if callable(write_batch):
        write_batch(rows)
        return
    write = getattr(cache, "set_position", None)
    if not callable(write):
        raise TypeError(
            "venue position adoption requires a cache with set_position(Position)"
        )
    log.warning(
        "venue position adoption apply is NOT atomic: cache lacks "
        "set_positions(Sequence[Position]); falling back to per-row writes"
    )
    for position in rows:
        write(position)


def _log_venue_adoption(plan: VenueAdoption, read: VenueRead) -> None:
    """Log the adoption, per instrument it acted on and once for the summary."""
    if plan.is_empty:
        log.info(
            "venue position adoption: no changes (venue=%d positions)",
            len(read.positions),
        )
        return
    for row in plan.as_log_rows():
        log.warning("venue position adoption: %s", row)
    log.warning(
        "venue position adoption complete: adopted=%d retained=%d cleared=%d "
        "skipped=%d",
        len(plan.positions),
        len(plan.retained),
        len(plan.cleared),
        len(plan.skipped),
    )


#: The metric name the monitor emits per adopted instrument, declared here so the
#: safety surface and its tests agree on the name without one importing the other.
METRIC_ADOPTED = "venue_adoption_adopted_total"
#: The metric name the monitor emits per cleared instrument.
METRIC_CLEARED = "venue_adoption_cleared_total"
#: The metric name the monitor emits per skipped instrument, labelled by reason.
METRIC_SKIPPED = "venue_adoption_skipped_total"
#: The metric name the monitor emits per refused adoption, labelled by reason.
METRIC_REFUSED = "venue_adoption_refused_total"
#: The metric name the monitor emits per failed venue read.
METRIC_READ_FAILED = "venue_read_failed_total"
#: The gauge of how many instruments the last adoption wrote.
METRIC_LAST_ADOPTED = "venue_adoption_last_adopted"


@dataclass(frozen=True, slots=True)
class ReconciliationMonitorSeams:
    """The two callables a reconciliation monitor needs, bound to a live session.

    ``tradex_runtime`` owns the timer but may not import this module
    (``tests/test_import_boundaries.py`` pins ``runtime`` below ``interfaces``),
    so the adoption and the reconciliation are handed to the monitor as plain
    callables and the binding lives here. This is the same layering as
    :mod:`tradex_interfaces.safety_report_store`: ``interfaces`` is the layer that
    sees the execution book and the analytics promotion bar, so the join belongs
    here rather than in a package that can only see one side.

    The seams are deliberately *closures over a session*, not a protocol the
    monitor implements: the monitor's contract is "call these, never die", and a
    class hierarchy here would be an indirection the runtime package could not
    import anyway.
    """

    reconcile: Callable[[], Sequence[Any]]
    adopt: Callable[[], AdoptionOutcome]


def build_reconciliation_monitor_seams(
    *,
    broker: Any,
    cache: Any,
    engine: Any | None = None,
    adopt: bool = True,
    clock: Callable[[], datetime] | None = None,
) -> ReconciliationMonitorSeams:
    """Bind the adoption and the reconciliation to one live session.

    The seams close over the objects that already own the state:

    * :meth:`ExecutionEngine.reconcile` runs the drift comparison and returns the
      items. It is side-effect free, so calling it from a timer changes nothing
      until the adoption acts.
    * :func:`adopt_venue_positions` is the mutation. It reads the venue through a
      sealed :class:`VenueRead`, refuses a failed/partial/stale read, derives the
      plan and applies it through ``cache.set_position``.

    ``known_instruments`` is derived from the book on every adoption rather than
    captured once, because the set the book holds is exactly what a partial read
    must be measured against and it changes as positions open and close. The
    unsettled set is derived from the engine's *orders* — the book's own in-flight
    state, which the venue's snapshot cannot yet have seen.
    """
    last_read_at: datetime | None = None

    def _unsettled() -> frozenset[str]:
        if engine is None:
            return frozenset()
        orders = getattr(engine, "all_orders", None)
        if not callable(orders):
            orders = getattr(getattr(engine, "cache", None), "all_orders", None)
        if not callable(orders):
            return frozenset()
        try:
            return unsettled_local_instruments(list(orders()))
        except Exception:  # noqa: BLE001 - a monitor that dies stops reconciling
            log.warning("could not read orders for unsettled adoption guard")
            return frozenset()

    def _local() -> list[Any]:
        positions = getattr(cache, "all_positions", None)
        if not callable(positions):
            return []
        try:
            return list(positions())
        except Exception:  # noqa: BLE001 - never take the poll down
            log.warning("could not read local positions for adoption")
            return []

    def _adopt() -> AdoptionOutcome:
        nonlocal last_read_at
        local_positions = _local()
        outcome = adopt_venue_positions(
            broker=broker,
            local_positions=local_positions,
            cache=cache,
            unsettled_instruments=_unsettled(),
            known_instruments=[
                _position_instrument_id(p) for p in local_positions
                if _position_instrument_id(p) is not None
            ],
            last_read_at=last_read_at,
            now=clock() if clock is not None else None,
            adopt=adopt,
        )
        # The freshness baseline advances only on a usable read. A failed or
        # partial read must not become the bar a later read is judged stale
        # against, or a read carrying no data would refuse every read after it.
        read = outcome.read
        if read is not None and read.ok and read.read_at is not None:
            last_read_at = read.read_at
        return outcome

    def _reconcile() -> Sequence[Any]:
        if engine is None:
            return ()
        try:
            return tuple(engine.reconcile())
        except Exception:  # noqa: BLE001 - a monitor that dies stops reconciling
            log.warning("reconciliation comparison failed", exc_info=True)
            return ()

    return ReconciliationMonitorSeams(reconcile=_reconcile, adopt=_adopt)


__all__ = [
    "BOOT_REFUSING_SEVERITIES",
    "FUNDS_DRIFT_THRESHOLD_INR",
    "METRIC_ADOPTED",
    "METRIC_CLEARED",
    "METRIC_LAST_ADOPTED",
    "METRIC_READ_FAILED",
    "METRIC_REFUSED",
    "METRIC_SKIPPED",
    "SEVERITIES",
    "SKIP_UNREADABLE_INSTRUMENT",
    "SKIP_UNREADABLE_POSITION",
    "SKIP_UNREADABLE_QUANTITY",
    "SKIP_UNSETTLED_FILL",
    "SKIP_VENUE_OMITTED",
    "AdoptionOutcome",
    "DriftFact",
    "DriftKind",
    "PromotionStatus",
    "ReconciliationMonitorSeams",
    "ReconciliationSafety",
    "SafetyReport",
    "StrategySafety",
    "UnknownOrder",
    "VenueAdoption",
    "VenueRead",
    "adopt_venue_positions",
    "build_reconciliation_monitor_seams",
    "build_safety_report",
    "derive_severity",
    "derive_venue_adoption",
    "drift_facts",
    "facts_from_drift_item",
    "feed_state",
    "promotion_status",
    "read_venue_positions",
    "refuses_boot",
    "severity_totals",
    "unsettled_local_instruments",
    "unresolved_unknown_orders",
]
