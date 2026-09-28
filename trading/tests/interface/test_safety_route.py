"""AC-17 — one route answers the whole safety question.

The severity rule is the load-bearing part, and AC-17 is explicit that it is
*derived* rather than entered. That makes the unit tests here the primary
evidence, not a formality: if the route could be made to serve a stored
verdict, the derivation would still pass while the property AC-17 is written
against would be gone. The negative controls are therefore aimed at the two
failure shapes that matter — a drift classified LOW that should be HIGH, and a
LOW-only reconciliation quietly letting a session start.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.events import OrderPlaced
from tradex_domain.execution import Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity
from tradex_execution.reconciliation import DriftItem, DriftSeverity
from tradex_interfaces.safety import (
    BOOT_REFUSING_SEVERITIES,
    DriftFact,
    DriftKind,
    PromotionStatus,
    build_safety_report,
    derive_severity,
    facts_from_drift_item,
    promotion_status,
    refuses_boot,
    severity_totals,
    unresolved_unknown_orders,
)

INSTRUMENT = Equity.of("NSE", "RELIANCE")
NOW = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)

fastapi = pytest.importorskip("fastapi")


# ---------------------------------------------------------------------------
# The derived severity rule, one branch at a time
# ---------------------------------------------------------------------------


class TestDeriveSeverity:
    """Every branch AC-17 names, plus the boundary between HIGH and LOW."""

    def test_missing_venue_read_is_critical(self):
        assert derive_severity(
            DriftFact(kind=DriftKind.ORDER, venue_read_missing=True),
        ) == "CRITICAL"

    def test_quantity_mismatch_is_high(self):
        assert derive_severity(
            DriftFact(kind=DriftKind.POSITION, quantity_mismatch=True),
        ) == "HIGH"

    def test_status_mismatch_is_high(self):
        """AC-17 says a *status* mismatch is HIGH.

        The engine files a status disagreement as DriftSeverity.LOW. Asserting
        the derived value here rather than the stored one is the whole point:
        if the derivation ever started reading the stored field, this fails
        while the route kept working.
        """
        assert derive_severity(
            DriftFact(kind=DriftKind.ORDER, status_mismatch=True),
        ) == "HIGH"

    def test_funds_difference_above_one_rupee_is_high(self):
        assert derive_severity(
            DriftFact(kind=DriftKind.FUNDS, funds_difference=Decimal("1.01")),
        ) == "HIGH"

    def test_funds_difference_of_exactly_one_rupee_is_low(self):
        """'above one rupee' is strict. One rupee is not above one rupee.

        The boundary is the whole content of the rule — an inclusive ``>=``
        would refuse boot over a rupee, which is a decision the spec did not
        make.
        """
        assert derive_severity(
            DriftFact(kind=DriftKind.FUNDS, funds_difference=Decimal("1")),
        ) == "LOW"

    def test_anything_below_is_low(self):
        assert derive_severity(DriftFact(kind=DriftKind.POSITION)) == "LOW"

    def test_missing_venue_read_outranks_a_quantity_mismatch(self):
        """A missing read is CRITICAL even though it also looks like a mismatch.

        The venue side is zero *because nothing came back*, so reading it as
        "we hold 100, they hold 0" would file the only genuinely unknown state
        as a plain HIGH and let it boot.
        """
        assert derive_severity(
            DriftFact(
                kind=DriftKind.ORDER,
                quantity_mismatch=True,
                venue_read_missing=True,
            ),
        ) == "CRITICAL"

    def test_derivation_never_reads_a_stored_severity(self):
        """A DriftFact has no severity to read, so the omission is structural."""
        assert not hasattr(DriftFact(kind=DriftKind.POSITION), "severity")
        assert "severity" not in DriftFact.__slots__


class TestRefusesBoot:
    """'Only HIGH and CRITICAL refuse boot' — and LOW really does not."""

    def test_high_and_critical_refuse(self):
        assert refuses_boot("HIGH") is True
        assert refuses_boot("CRITICAL") is True

    def test_low_does_not_refuse(self):
        assert refuses_boot("LOW") is False

    def test_the_refusing_set_is_exactly_high_and_critical(self):
        assert BOOT_REFUSING_SEVERITIES == {"HIGH", "CRITICAL"}

    def test_every_derived_severity_is_covered_by_the_rule(self):
        """No derived value may be a severity the boot rule ignores.

        A severity that derived fine but was not in the refusing set would be
        a hole: it would appear in the totals and never stop anything.
        """
        produced = {
            derive_severity(DriftFact(kind=DriftKind.FUNDS, funds_difference=Decimal(n)))
            for n in ("0", "1", "1000")
        } | {"CRITICAL"}
        assert produced <= BOOT_REFUSING_SEVERITIES | {"LOW"}


class TestSeverityTotals:
    def test_totals_count_by_derived_severity(self):
        facts = [
            DriftFact(kind=DriftKind.POSITION, venue_read_missing=True),
            DriftFact(kind=DriftKind.POSITION, quantity_mismatch=True),
            DriftFact(kind=DriftKind.FUNDS, funds_difference=Decimal("50")),
            DriftFact(kind=DriftKind.POSITION),
        ]
        assert severity_totals(facts) == {"LOW": 1, "HIGH": 2, "CRITICAL": 1}

    def test_totals_always_carry_every_severity_at_zero(self):
        """A consumer renders a row per severity; a missing key is a bug."""
        assert severity_totals([]) == {"LOW": 0, "HIGH": 0, "CRITICAL": 0}


# ---------------------------------------------------------------------------
# Promotion status mapping
# ---------------------------------------------------------------------------


class TestPromotionStatusMapping:
    """AC-17's three answers, mapped off the store's vocabulary."""

    def test_passed_becomes_promotable(self):
        assert promotion_status("passed") == "promotable"

    def test_not_promoted_becomes_not_promotable(self):
        assert promotion_status("not_promoted") == "not_promotable"

    def test_not_evaluated_passes_through(self):
        assert promotion_status("not_evaluated") == "not_evaluated"

    def test_the_three_answers_are_the_ones_ac17_names(self):
        assert {s.value for s in PromotionStatus} == {
            "promotable", "not_evaluated", "not_promotable",
        }

    def test_an_unmapped_store_status_raises_rather_than_defaulting(self):
        """A fourth status would change what the answers mean.

        Defaulting it to anything in particular is how a strategy gets a
        verdict it was never given, so it raises.
        """
        with pytest.raises(ValueError, match="unmapped promotion status"):
            promotion_status("retired")


# ---------------------------------------------------------------------------
# Reading observations off a DriftItem
# ---------------------------------------------------------------------------


class TestFactsFromDriftItem:
    """The translation reads measurements, never the stored severity."""

    def test_missing_broker_order_is_a_missing_venue_read(self):
        fact = facts_from_drift_item(
            DriftItem(
                kind="order", key="o-1", severity=DriftSeverity.HIGH,
                reason="missing broker order", local=object(), remote=None,
            ),
        )
        assert fact.venue_read_missing is True
        assert derive_severity(fact) == "CRITICAL"

    def test_a_position_quantity_difference_is_a_quantity_mismatch(self):
        fact = facts_from_drift_item(
            DriftItem(symbol="NSE:RELIANCE", local_quantity=Decimal("100"),
                      broker_quantity=Decimal("0"), diff=Decimal("100")),
        )
        assert fact.quantity_mismatch is True
        assert derive_severity(fact) == "HIGH"

    def test_a_status_lag_is_a_status_mismatch_not_low(self):
        """The engine stores this as LOW; AC-17 derives HIGH from it."""
        item = DriftItem(
            kind="order", key="o-1", severity=DriftSeverity.LOW,
            reason="status lag", local=None, remote=None,
        )
        fact = facts_from_drift_item(item)
        assert item.severity is DriftSeverity.LOW
        assert derive_severity(fact) == "HIGH"

    def test_undrifted_position_facts_as_low(self):
        fact = facts_from_drift_item(DriftItem(symbol="NSE:RELIANCE", diff=Decimal("0")))
        assert derive_severity(fact) == "LOW"


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


def _client(session=None):
    from fastapi.testclient import TestClient
    from tradex_interfaces.fastapi_app import create_app

    return TestClient(create_app(session=session))


def _unknown_order(order_id: str = "ord-unknown-1") -> Order:
    return Order(
        order_id=OrderId(order_id),
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("25")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.UNKNOWN,
        correlation_id=CorrelationId("cid-abc-123"),
    )


class _Cache:
    def __init__(self, orders): self._orders = orders
    def all_orders(self): return list(self._orders)
    def all_positions(self): return []


class _Engine:
    def __init__(self, orders): self.cache = _Cache(orders)
    def reconcile(self, **kwargs): return []


class _EventStore:
    def __init__(self, events): self._events = events
    def replay(self, stream="orders"): return iter(list(self._events))


class _Session:
    """A session carrying only what the safety surface reads."""

    def __init__(self, *, orders=(), events=(), drifts=None, registry=None, store=None):
        self.engine = _Engine(orders)
        self.event_store = _EventStore(events)
        self.feed_monitor = None
        self.strategy_registry = registry
        self.validation_report_store = store
        self.last_reconciliation = drifts
        self.broker = type("B", (), {"get_positions": staticmethod(lambda: [])})()


class TestRouteHappyPath:
    def test_the_route_answers_without_a_session(self):
        """An unbooted server is a safety fact, not a 500."""
        response = _client(None).get("/api/charts/safety")
        assert response.status_code == 200
        body = response.json()
        assert body["feed"]["state"] is None
        assert body["unknown_orders"] == []
        assert body["reconciliation"]["totals_by_severity"] == {
            "LOW": 0, "HIGH": 0, "CRITICAL": 0,
        }
        assert body["strategies"] == []

    def test_the_response_carries_every_ac17_section(self):
        body = _client(None).get("/api/charts/safety").json()
        assert {
            "feed", "unknown_orders", "reconciliation", "strategies",
            "boot_refused", "reconciliation_checked",
        } <= set(body)

    def test_a_checked_session_reports_when_it_was_last_checked(self):
        checked = datetime(2026, 9, 28, 9, 30, tzinfo=UTC)
        session = _Session(
            events=[_reconciliation_event(checked)],
            drifts=[],
        )
        body = _client(session).get("/api/charts/safety").json()
        assert body["reconciliation"]["last_checked_at"] == checked.isoformat()
        assert body["reconciliation_checked"] is True

    def test_an_unchecked_session_is_not_reported_as_clean(self):
        """Never checked is not the same as checked and found nothing.

        The route must not let 'no reconciliation has run' read as a pass.
        """
        body = _client(_Session()).get("/api/charts/safety").json()
        assert body["reconciliation_checked"] is False
        assert body["reconciliation"]["last_checked_at"] is None
        assert body["boot_refused"] is False


def _reconciliation_event(at: datetime):
    from tradex_domain.events import ReconciliationResult

    return ReconciliationResult(drift_count=0, timestamp=at)


class TestUnknownOrders:
    def test_an_unresolved_unknown_order_is_listed_with_age_and_correlation_id(self):
        # The route measures against wall-clock now, so the filed time is
        # relative to it rather than to the module's fixed NOW.
        filed = datetime.now(UTC) - timedelta(seconds=90)
        order = _unknown_order()
        session = _Session(
            orders=[order],
            events=[OrderPlaced(order=order, timestamp=filed)],
        )
        body = _client(session).get("/api/charts/safety").json()

        assert len(body["unknown_orders"]) == 1
        row = body["unknown_orders"][0]
        assert row["order_id"] == "ord-unknown-1"
        assert row["correlation_id"] == "cid-abc-123"
        assert row["age_seconds"] == pytest.approx(90.0, abs=5.0)
        assert row["since"] is not None

    def test_a_resolved_order_is_not_listed(self):
        """Only *unresolved* UNKNOWN orders are the operator's problem.

        Negative control: an order that reached FILLED must not appear.
        """
        filled = _unknown_order().transition_to(OrderStatus.FILLED)
        session = _Session(orders=[filled], events=[])
        body = _client(session).get("/api/charts/safety").json()
        assert body["unknown_orders"] == []

    def test_an_unknown_order_with_no_recorded_time_reports_no_age(self):
        """Absent is not zero.

        An order whose filing time is not in the log must not read as a fresh
        one — an age of zero would understate exactly the orders an operator
        most needs to see.
        """
        session = _Session(orders=[_unknown_order()], events=[])
        row = _client(session).get("/api/charts/safety").json()["unknown_orders"][0]
        assert row["age_seconds"] is None
        assert row["since"] is None

    def test_a_cancelled_unknown_order_is_not_an_unknown_one(self):
        """A resolution leaves the book; only *unresolved* ones are listed.

        ``transition_to`` enforces the legal-transition table, so resolving an
        UNKNOWN order is a CANCELLED, not a move back to SUBMITTED.
        """
        order = _unknown_order().transition_to(OrderStatus.CANCELLED)
        body = _client(_Session(orders=[order], events=[])).get(
            "/api/charts/safety",
        ).json()
        assert body["unknown_orders"] == []


class TestReconciliationSeverities:
    """The totals are derived, and only HIGH/CRITICAL refuse."""

    def test_low_only_drift_does_not_refuse_boot(self):
        """NEGATIVE CONTROL: the rule that must NOT be overstated.

        A LOW-only reconciliation is the case AC-17 exempts. If the derivation
        ever escalated LOW, this fails — and with it, every operator who has a
        paise-level discrepancy and still needs to trade.
        """
        drifts = [DriftItem(symbol="NSE:RELIANCE", diff=Decimal("0"))]
        session = _Session(
            events=[_reconciliation_event(NOW)], drifts=drifts,
        )
        body = _client(session).get("/api/charts/safety").json()
        assert body["reconciliation"]["totals_by_severity"]["LOW"] == 1
        assert body["boot_refused"] is False

    def test_high_drift_refuses_boot(self):
        drifts = [
            DriftItem(kind="order", key="o-1", reason="status lag",
                      severity=DriftSeverity.LOW),
        ]
        session = _Session(
            events=[_reconciliation_event(NOW)], drifts=drifts,
        )
        body = _client(session).get("/api/charts/safety").json()
        assert body["reconciliation"]["totals_by_severity"]["HIGH"] == 1
        assert body["boot_refused"] is True

    def test_a_missing_venue_read_refuses_boot_as_critical(self):
        drifts = [
            DriftItem(kind="order", key="o-1", reason="missing broker order",
                      local=object(), remote=None, severity=DriftSeverity.HIGH),
        ]
        session = _Session(
            events=[_reconciliation_event(NOW)], drifts=drifts,
        )
        body = _client(session).get("/api/charts/safety").json()
        assert body["reconciliation"]["totals_by_severity"]["CRITICAL"] == 1
        assert body["boot_refused"] is True

    def test_the_stored_severity_is_never_what_is_reported(self):
        """A stored CRITICAL on a mere status lag must still be derived HIGH.

        The inversion is deliberate and is the cleanest proof the number is
        computed: the item claims CRITICAL, the facts say HIGH, and the route
        must serve the facts.
        """
        drifts = [
            DriftItem(kind="order", key="o-1", reason="status lag",
                      severity=DriftSeverity.CRITICAL),
        ]
        session = _Session(
            events=[_reconciliation_event(NOW)], drifts=drifts,
        )
        body = _client(session).get("/api/charts/safety").json()
        totals = body["reconciliation"]["totals_by_severity"]
        assert totals == {"LOW": 0, "HIGH": 1, "CRITICAL": 0}

    def test_the_refusing_count_matches_the_boot_flag(self):
        drifts = [
            DriftItem(kind="order", key="o-1", reason="missing broker order",
                      local=object(), remote=None),
            DriftItem(symbol="NSE:RELIANCE", diff=Decimal("0")),
        ]
        session = _Session(
            events=[_reconciliation_event(NOW)], drifts=drifts,
        )
        body = _client(session).get("/api/charts/safety").json()
        assert body["reconciliation"]["refusing"] == 1
        assert body["boot_refused"] is True


class TestStrategyPromotionRows:
    """Per strategy: lifecycle, promotion status, report id, failures."""

    def _report(self, *, passing: bool):
        from tradex_analytics.validation_report import TradeValidationReport

        good = dict(
            report_id="rep-1", strategy_id="s1", parameter_set_hash="h1",
            oos_sharpe=2.5, oos_round_trips=140, oos_fills=190,
            oos_span_days=120.0,
            expectancy=25.0, profit_factor=1.9, max_drawdown_pct=-8.0,
            deflated_sharpe=1.2, param_stability_share_of_grid=0.7,
            net_pnl_at_2x_costs=500.0, bar_frequency="1d",
        )
        if passing:
            return TradeValidationReport(**good)
        return TradeValidationReport(
            **{**good, "oos_sharpe": 0.1, "oos_round_trips": 3, "oos_span_days": 4.0},
        )

    def _store(self, reports):
        from tradex_execution.validation_report_store import ValidationReportStore

        store = ValidationReportStore(
            ":memory:",
            rehydrate=lambda kwargs: self._report(passing=True).__class__(**kwargs),
        )
        for report in reports:
            store.save(report)
        return store

    def _runtime(self, strategy_id: str, lifecycle: str):
        from tradex_strategy.runtime import StrategyLifecycle

        runtime = type("R", (), {})()
        runtime.artifact = type(
            "A", (), {"strategy_id": strategy_id, "version": "1.0.0"},
        )()
        runtime.lifecycle = StrategyLifecycle(lifecycle)
        return runtime

    def _registry(self, runtimes):
        registry = type("Reg", (), {})()
        registry.list_all = lambda: list(runtimes)
        return registry

    def test_a_promotable_strategy_is_reported_promotable(self):
        report = self._report(passing=True)
        session = _Session(
            registry=self._registry([self._runtime("s1", "running")]),
            store=self._store([report]),
        )
        row = _client(session).get("/api/charts/safety").json()["strategies"][0]
        assert row["strategy_id"] == "s1"
        assert row["lifecycle_state"] == "running"
        assert row["promotion_status"] == "promotable"
        assert row["report_id"] == "rep-1"
        assert row["promotion_failures"] == []

    def test_a_not_promotable_strategy_carries_its_failure_reasons(self):
        """A rejected strategy must say WHY, not merely that it failed."""
        failing = self._report(passing=False)
        session = _Session(
            registry=self._registry([self._runtime("s1", "stopped")]),
            store=self._store([failing]),
        )
        row = _client(session).get("/api/charts/safety").json()["strategies"][0]
        assert row["promotion_status"] == "not_promotable"
        assert row["report_id"] == "rep-1"
        assert row["promotion_failures"], "a not_promotable strategy must name its failures"
        assert any("oos_sharpe" in reason for reason in row["promotion_failures"])
        assert any("oos_round_trips" in reason for reason in row["promotion_failures"])

    def test_a_strategy_with_no_report_is_not_evaluated(self):
        """NEGATIVE CONTROL against the not-promotable answer.

        These are different operational facts — nobody ran it, versus it ran
        and failed — and merging them would report an un-run strategy as a
        rejected one.
        """
        session = _Session(
            registry=self._registry([self._runtime("s-unrun", "created")]),
            store=self._store([]),
        )
        row = _client(session).get("/api/charts/safety").json()["strategies"][0]
        assert row["promotion_status"] == "not_evaluated"
        assert row["report_id"] is None
        assert row["promotion_failures"] == []
        assert row["lifecycle_state"] == "created"

    def test_a_strategy_with_no_store_at_all_is_not_evaluated(self):
        session = _Session(registry=self._registry([self._runtime("s1", "created")]))
        row = _client(session).get("/api/charts/safety").json()["strategies"][0]
        assert row["promotion_status"] == "not_evaluated"

    def test_the_store_rehydrates_rather_than_trusting_a_verdict_column(self):
        """A verdict column edited in the database cannot make it promotable.

        The surface reads through ``promotion_status``, which rehydrates the
        report and re-applies the bar to the stored *metrics*. A row whose
        ``passed`` flag says 1 while its metrics miss the bar still answers
        not-promotable — which is what makes the safety surface worth reading
        when a strategy is close to the line.
        """
        store = self._store([])
        store.save(self._report(passing=True))
        assert store.promotion_status("s1") == "passed"

        store.connection().execute(
            "UPDATE tradex_validation_report SET oos_sharpe = 0.01, passed = 1 "
            "WHERE strategy_id = ?",
            ("s1",),
        )
        store.connection().commit()

        # The stored flag still claims a pass; the recomputed verdict does not.
        row = store.connection().execute(
            "SELECT passed FROM tradex_validation_report WHERE strategy_id = ?",
            ("s1",),
        ).fetchone()
        assert int(row[0]) == 1
        assert store.promotion_status("s1") == "not_promoted"
        assert store.latest("s1").failures

    def test_a_saved_report_round_trips_through_the_store(self):
        """The surface's report id is the stored one, not a re-derived string."""
        store = self._store([self._report(passing=True)])
        assert store.latest("s1").report_id == "rep-1"


    def test_lifecycle_is_reported_per_strategy(self):
        """Each strategy carries its own state; the registry is not flattened."""
        runtimes = [self._runtime("a", "running"), self._runtime("b", "failed")]
        session = _Session(registry=self._registry(runtimes), store=self._store([]))
        rows = {r["strategy_id"]: r for r in _client(session).get(
            "/api/charts/safety",
        ).json()["strategies"]}
        assert rows["a"]["lifecycle_state"] == "running"
        assert rows["b"]["lifecycle_state"] == "failed"


# ---------------------------------------------------------------------------
# The assembly, without HTTP
# ---------------------------------------------------------------------------



class TestBuildSafetyReport:
    def test_the_helper_is_usable_without_a_session(self):
        """The derivation is testable without FastAPI and without a session."""
        report = build_safety_report(None)
        assert report.boot_refused is False
        assert report.reconciliation_checked is False
        assert report.to_wire()["strategies"] == []

    def test_age_is_measured_from_the_transition_not_the_order(self):
        filed = NOW - timedelta(minutes=5)
        order = _unknown_order()
        rows = unresolved_unknown_orders(
            [order], {"ord-unknown-1": filed}, now=NOW,
        )
        assert rows[0].age_seconds == pytest.approx(300.0)

    def test_a_future_timestamp_does_not_yield_a_negative_age(self):
        rows = unresolved_unknown_orders(
            [_unknown_order()], {"ord-unknown-1": NOW + timedelta(hours=1)}, now=NOW,
        )
        assert rows[0].age_seconds == 0.0
