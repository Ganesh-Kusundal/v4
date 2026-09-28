"""AC-17 / lane 2e — the safety route reports a REAL session, not nulls.

``trading/tests/interface/test_safety_route.py`` proves the severity rule and
the null-tolerance of the surface. It builds its own ``_Session`` stub, so it
cannot catch the failure this file exists for: **a genuinely booted
``TradingSession`` has none of the four accessors the route reads**, and the
route's defensive ``getattr(..., None)`` turned each of them into a silent
``None``.

The four were:

1. ``session.event_store`` — the route read ``None``, replayed no events, and
   so reported ``age_seconds=None`` for every unresolved UNKNOWN order and
   ``last_checked_at=None`` forever.
2. ``session.validation_report_store`` — read ``None``, so every strategy
   answered ``not_evaluated`` with no report id, forever.
3. ``session.last_reconciliation`` — absent, so the route fell through to
   re-deriving drift and reported what a *fresh* comparison found rather than
   what the session's own reconcile had found.
4. ``session.strategy_registry`` — present, but as a zero-arg **method**
   ``strategy_registry()`` [REF-5], not a property. ``getattr`` on a bound
   method never raises; the ``getattr(registry, "list_all", list)`` fallback
   silently substituted the builtin ``list``, and ``list()`` is empty. A booted
   session with eight registered strategies reported **zero**, and reported it
   as "no strategies exist".

Every test here boots a real session through the real composition root
(``tradex_runtime.startup.boot``) with real SQLite stores attached, and asserts
the route returns actual state. Each is a negative control: it fails if the
corresponding accessor returns ``None``, which is precisely the defect.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tradex_analytics.validation_report import TradeValidationReport
from tradex_brokers.paper.adapter import PaperBroker
from tradex_config.schema import AppConfig
from tradex_domain import UNBOUNDED
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.events import OrderPlaced
from tradex_domain.execution import Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity
from tradex_execution.sqlite_event_store import SQLiteEventStore
from tradex_execution.validation_report_store import ValidationReportStore
from tradex_interfaces.safety import build_safety_report
from tradex_interfaces.safety_report_store import build_validation_report_store
from tradex_runtime.startup import boot

INSTRUMENT = Equity.of("NSE", "RELIANCE")

fastapi = pytest.importorskip("fastapi")


# ---------------------------------------------------------------------------
# A real session, booted through the real composition root
# ---------------------------------------------------------------------------


def _unbounded_risk(cfg: AppConfig) -> AppConfig:
    """Paper boot refuses without a named risk limit; UNBOUNDED is the opt-out."""
    return replace(
        cfg,
        risk=replace(
            cfg.risk,
            max_order_value=UNBOUNDED,
            max_position_value=UNBOUNDED,
            max_orders_per_minute=UNBOUNDED,
            max_daily_loss_amt=UNBOUNDED,
            max_drawdown_pct=UNBOUNDED,
        ),
    )


def _rehydrate(kwargs):
    """The composition root's rehydrator, supplied from the test layer.

    AC-13 makes ``tradex_replay.validation`` the single writer of a
    ``TradeValidationReport`` in production, and
    ``test_validation_entry_point.py`` fails the build on a second construction
    site under a production path. A test is not such a site, so the wiring is
    proven here; the production builder takes the callable as a parameter for
    exactly this reason.
    """
    return TradeValidationReport(**kwargs)


@pytest.fixture
def live_session(tmp_path):
    """A real paper session with real SQLite stores, plus the store handle.

    ``execution.auto_register_examples`` is ON so the registry is genuinely
    populated: with it off the registry is empty and a test could not tell
    "the route read the registry" from "the registry had nothing in it", which
    is exactly the ambiguity that hid the ``strategy_registry`` bug.
    """
    db = str(tmp_path / "safety.db")
    cfg = _unbounded_risk(AppConfig())
    cfg = replace(
        cfg,
        mode="paper",
        execution=replace(cfg.execution, auto_register_examples=True),
        persistence=replace(cfg.persistence, path=db),
    )
    store = build_validation_report_store(db, _rehydrate)
    assert store is not None, "a configured persistence path must yield a store"
    session = boot(
        cfg,
        broker=PaperBroker(),
        wire_strategies=True,
        validation_report_store=store,
    )
    try:
        yield session, store, db
    finally:
        session.stop()


def _passing_report(strategy_id: str) -> TradeValidationReport:
    return TradeValidationReport(
        report_id="rep-live-1",
        strategy_id=strategy_id,
        parameter_set_hash="h1",
        oos_sharpe=2.5,
        oos_round_trips=140,
        oos_fills=190,
        oos_span_days=120.0,
        expectancy=25.0,
        profit_factor=1.9,
        max_drawdown_pct=-8.0,
        deflated_sharpe=1.2,
        param_stability_share_of_grid=0.7,
        net_pnl_at_2x_costs=500.0,
        bar_frequency="1d",
    )


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


# ---------------------------------------------------------------------------
# 1. The accessors themselves are present and are the real objects
# ---------------------------------------------------------------------------


class TestAccessorsAreWired:
    """NEGATIVE CONTROL: each accessor must be the real object, not ``None``.

    These are the assertions that fail on the pre-lane-2e wiring. ``None`` is
    what the route silently degraded to, so asserting ``is not None`` is
    asserting the defect is gone.
    """

    def test_event_store_is_the_real_durable_store(self, live_session):
        session, _, _ = live_session
        assert session.event_store is not None, (
            "event_store is None: the safety route replays no events, so every "
            "UNKNOWN-order age and last_checked_at is a permanent null"
        )
        assert isinstance(session.event_store, SQLiteEventStore)

    def test_validation_report_store_is_the_real_store(self, live_session):
        session, store, _ = live_session
        assert session.validation_report_store is not None, (
            "validation_report_store is None: every strategy reads "
            "not_evaluated with no report id, forever"
        )
        assert session.validation_report_store is store
        assert isinstance(session.validation_report_store, ValidationReportStore)

    def test_strategy_registry_is_reachable_and_populated(self, live_session):
        session, _, _ = live_session
        registry = session.strategy_registry()
        assert registry is not None
        assert registry.list_all(), (
            "the registry is empty, so a strategy-row assertion below would "
            "pass vacuously"
        )

    def test_a_session_with_no_persistence_reports_absent_not_a_fake_store(
        self, tmp_path,
    ):
        """No configured DB must yield ``None``, never an in-memory store.

        An in-memory store answers ``not_evaluated`` for every strategy
        forever, and the route would report that as a real promotion standing
        rather than as the absence of any record.
        """
        assert build_validation_report_store(None, _rehydrate) is None
        assert build_validation_report_store("", _rehydrate) is None

    def test_the_builder_refuses_a_non_callable_rehydrator(self, tmp_path):
        """The rehydrator is required, and the store enforces it.

        A store built with a missing rehydrator could only answer a promotion
        question by trusting the stored verdict column — the exact laundering
        AC-13 exists to prevent — so the store raises rather than defaulting.
        """
        from tradex_execution.validation_report_store import (
            ValidationReportStore,
            ValidationReportStoreError,
        )

        with pytest.raises(ValidationReportStoreError, match="rehydrate"):
            ValidationReportStore(str(tmp_path / "x.db"), rehydrate=None)

    def test_no_persistence_session_leaves_the_accessor_none(self):
        """A session booted with no persistence path has no event store.

        That is the honest answer, and the route must render it as absent
        rather than inventing an empty log.
        """
        cfg = _unbounded_risk(AppConfig())
        cfg = replace(cfg, mode="paper")
        session = boot(cfg, broker=PaperBroker(), wire_strategies=False)
        try:
            assert session.event_store is None
            report = build_safety_report(session)
            assert report.unknown_orders == ()
            assert report.reconciliation_checked is False
        finally:
            session.stop()


# ---------------------------------------------------------------------------
# 2. The route reports real state (the negative control that matters)
# ---------------------------------------------------------------------------


class TestRouteReportsRealState:
    """A booted session must produce actual state at every degraded point."""

    def test_the_route_reports_a_row_for_every_registered_strategy(self, live_session):
        session, _, _ = live_session
        report = build_safety_report(session)

        registered = {r.artifact.strategy_id for r in session.strategy_registry().list_all()}
        reported = {row.strategy_id for row in report.strategies}
        assert reported == registered, (
            "the route dropped registered strategies. Before the "
            "strategy_registry shape fix this was 0 rows against 8 registered."
        )
        assert reported, "sanity: the registry is populated, so rows must exist"

    def test_a_saved_promotion_report_reaches_the_route(self, live_session):
        """A report persisted beside the event log must be readable and real.

        Not ``not_evaluated``: the store is consulted, rehydrates the report
        and re-applies the bar, so a stored, passing report answers
        ``promotable`` with its real id.
        """
        session, store, _ = live_session
        strategy_id = session.strategy_registry().list_all()[0].artifact.strategy_id
        store.save(_passing_report(strategy_id))

        rows = {r.strategy_id: r for r in build_safety_report(session).strategies}
        row = rows[strategy_id]
        assert row.promotion_status == "promotable", (
            "a stored passing report must read as promotable; not_evaluated "
            "means the route never consulted the store"
        )
        assert row.report_id == "rep-live-1"
        assert row.promotion_failures == ()

    def test_a_strategy_with_no_saved_report_is_not_evaluated(self, live_session):
        """NEGATIVE CONTROL against the not-promotable answer.

        A strategy with no report is *unrun*, which is a different
        operational fact from one that ran and failed. The fixture saves a
        report for one strategy only, so the other seven must read
        ``not_evaluated`` — proving the store is consulted per strategy and
        that a missing row is not read as a failure.
        """
        session, store, _ = live_session
        registered = [r.artifact.strategy_id for r in session.strategy_registry().list_all()]
        assert len(registered) > 1, "sanity: the fixture needs a mix"
        store.save(_passing_report(registered[0]))

        rows = {r.strategy_id: r for r in build_safety_report(session).strategies}
        assert rows[registered[0]].promotion_status == "promotable"
        for strategy_id in registered[1:]:
            assert rows[strategy_id].promotion_status == "not_evaluated", (
                "a strategy with no report is unrun, not rejected"
            )
            assert rows[strategy_id].report_id is None
            assert rows[strategy_id].promotion_failures == ()

    def test_the_route_dates_an_unknown_order_from_the_durable_log(self, live_session):
        """An UNKNOWN order's age comes from the log, not from ``None``.

        This is the end-to-end proof that ``event_store`` is wired: before it,
        the route replayed no events and reported ``age_seconds=None`` — the
        single most dangerous thing a safety surface can say about an order it
        cannot resolve.
        """
        session, _, _ = live_session
        filed = datetime.now(UTC) - timedelta(seconds=90)
        order = _unknown_order()
        session.engine.cache.set_order(order)
        session.event_store.append(OrderPlaced(order=order, timestamp=filed))

        rows = build_safety_report(session).unknown_orders
        assert len(rows) == 1, "the UNKNOWN order must be listed"
        row = rows[0]
        assert row.age_seconds is not None, (
            "age_seconds is None: the event log was never replayed"
        )
        assert row.age_seconds == pytest.approx(90.0, abs=10.0)
        assert row.since is not None

    def test_an_unknown_order_with_no_log_entry_reports_no_age(self, live_session):
        """NEGATIVE CONTROL: absent is not zero.

        Without this, a null age could be 'correct' by accident and the test
        above would prove nothing.
        """
        session, _, _ = live_session
        order = _unknown_order("ord-no-record")
        session.engine.cache.set_order(order)
        # Deliberately NOT appended to the event store.
        rows = build_safety_report(session).unknown_orders
        assert rows[0].age_seconds is None
        assert rows[0].since is None

    def test_retained_drift_is_what_the_route_reports(self, live_session):
        """Drift recorded on the session is the drift the route reports.

        Without the retention, the route re-derives from a *fresh* comparison
        and would answer "nothing found" for a session that had just halted on
        drift — the most misleading possible answer from a safety route.
        """
        from tradex_execution.reconciliation import DriftItem

        session, _, _ = live_session
        drifts = [
            DriftItem(kind="order", key="o-1", reason="status lag"),
        ]
        session.record_reconciliation(drifts)
        assert session.last_reconciliation is not None

        report = build_safety_report(session)
        assert report.reconciliation.totals["HIGH"] == 1
        assert report.boot_refused is True, (
            "retained HIGH drift must refuse boot; a re-derived comparison "
            "found nothing and the route would have said trading is fine"
        )

    def test_a_never_reconciled_session_is_still_reported_as_never_checked(
        self, live_session,
    ):
        """NEGATIVE CONTROL: retention must not make 'never' read as 'clean'.

        ``last_reconciliation`` is ``None`` until a reconcile runs, and the
        route distinguishes that from an empty result. A retention that
        defaulted to ``()`` would make this pass silently-wrong.
        """
        session, _, _ = live_session
        assert session.last_reconciliation is None
        report = build_safety_report(session)
        assert report.reconciliation_checked is False
        assert report.reconciliation.last_checked_at is None
        assert report.boot_refused is False

    def test_an_empty_reconciliation_is_checked_and_clean(self, live_session):
        """The other half of the distinction: ran, found nothing.

        ``()`` is not ``None``. Retaining an empty result is what makes
        'checked and clean' expressible at all.
        """
        session, _, _ = live_session
        session.record_reconciliation([])
        report = build_safety_report(session)
        assert report.reconciliation.totals == {"LOW": 0, "HIGH": 0, "CRITICAL": 0}
        assert report.boot_refused is False

    def test_the_feed_row_comes_from_the_sessions_monitor(self, live_session):
        """Paper has no feed monitor; the row must be absent, not fabricated.

        A fabricated ``ready: true`` here would be the worst possible lie, so
        the assertion is on the *shape* of the absence.
        """
        session, _, _ = live_session
        feed = build_safety_report(session).feed
        assert feed["state"] is None and feed["ready"] is None

    def test_reconciliation_run_retains_its_drift(self, tmp_path):
        """The engine's own reconcile path is what retains, end to end.

        Calls ``_run_startup_reconciliation`` directly (as its docstring invites)
        with a session, so the retention is proven on the real call path rather
        than on ``record_reconciliation`` called by hand.
        """
        from tradex_runtime.startup import _run_startup_reconciliation

        db = str(tmp_path / "recon.db")
        cfg = _unbounded_risk(AppConfig())
        cfg = replace(
            cfg, mode="paper", persistence=replace(cfg.persistence, path=db),
        )
        session = boot(cfg, broker=PaperBroker(), wire_strategies=False)
        try:
            assert session.last_reconciliation is None
            _run_startup_reconciliation(session.broker, session.engine, session)
            assert session.last_reconciliation is not None, (
                "the real reconcile path must retain its drift; otherwise the "
                "route re-derives and can disagree with the halt that just ran"
            )
            assert build_safety_report(session).reconciliation_checked is True
        finally:
            session.stop()

    def test_a_reconcile_makes_the_route_report_when_it_was_last_checked(
        self, live_session,
    ):
        """The reconciled session must report a real ``last_checked_at``.

        This is the ``system``-stream defect. ``ReconciliationResult`` is routed
        to ``system`` by the event store, and the route used to replay only
        ``orders`` — so even a *fully wired* store left ``last_checked_at`` a
        permanent ``None`` and the route said "never checked" about a session
        that had just reconciled. Asserting the accessor is present is not
        enough here; the marker has to be findable in the stream it was
        actually written to.
        """
        from tradex_runtime.startup import _run_startup_reconciliation

        session, _, _ = live_session
        assert build_safety_report(session).reconciliation_checked is False

        before = datetime.now(UTC)
        _run_startup_reconciliation(session.broker, session.engine, session)

        report = build_safety_report(session)
        assert report.reconciliation_checked is True, (
            "a session that just reconciled must not report last_checked_at=None"
        )
        assert report.reconciliation.last_checked_at is not None
        checked = datetime.fromisoformat(report.reconciliation.last_checked_at)
        assert checked >= before.replace(microsecond=0) - timedelta(seconds=1)

    def test_the_reconciliation_marker_is_read_from_the_stream_it_is_written_to(
        self, live_session,
    ):
        """Pins the routing fact itself, so a store change cannot hide it again.

        If a future store moved ``ReconciliationResult`` back into ``orders``,
        this still passes, but the route reading both streams would keep
        working — whereas a route reading only one would silently stop.
        """
        from tradex_domain.events import ReconciliationResult

        session, _, _ = live_session
        session.event_store.append(ReconciliationResult(drift_count=0))

        orders = [e for e in session.event_store.replay("orders")
                  if isinstance(e, ReconciliationResult)]
        system = [e for e in session.event_store.replay("system")
                  if isinstance(e, ReconciliationResult)]
        assert system, "ReconciliationResult must be durable in some stream"
        assert orders == [], (
            "if this now lands in 'orders' too, the two streams both carry it "
            "and the route's per-stream read is still correct"
        )
        assert build_safety_report(session).reconciliation_checked is True

    def test_a_reconnect_refreshes_the_retained_drift(self, tmp_path):
        """After a reconnect the safety surface reports the NEW comparison.

        The socket carries only incremental updates, so a reconnect is exactly
        when the local book can have diverged. Retaining the boot-time drift
        and never refreshing it would leave the surface reporting a
        reconciliation of a book that has since been re-read — stale is worse
        than absent for a safety answer.
        """
        from tradex_runtime.startup import _install_post_reconnect_reread

        db = str(tmp_path / "reconnect.db")
        cfg = _unbounded_risk(AppConfig())
        cfg = replace(
            cfg, mode="paper", persistence=replace(cfg.persistence, path=db),
        )
        session = boot(cfg, broker=PaperBroker(), wire_strategies=False)
        try:
            session.record_reconciliation([1, 2, 3])
            assert session.last_reconciliation is not None

            fired: list = []
            backend = type("B", (), {
                "set_reconnect_hook": staticmethod(lambda fn: fired.append(fn)),
            })()
            _install_post_reconnect_reread(
                session.broker, session.engine, backend, session,
            )
            assert fired, "the reconnect hook must be installed"
            fired[0]()

            assert session.last_reconciliation == (), (
                "the reconnect re-read must replace the retained drift with the "
                "comparison it just made"
            )
        finally:
            session.stop()


# ---------------------------------------------------------------------------
# 3. The strategy_registry shape, pinned at the unit level
# ---------------------------------------------------------------------------


class TestRegistryShape:
    """The bug: a bound method where the route expected the object.

    Pinned separately because it is the one defect that did not raise. Every
    ``getattr`` fallback in the route made it a silent empty list, so only an
    explicit assertion catches it.
    """

    def test_a_method_shaped_registry_is_still_read(self, live_session):
        from tradex_interfaces.safety import _registry_runtimes

        session, _, _ = live_session
        method_shaped = session.strategy_registry  # the bound METHOD, not called
        assert callable(method_shaped)
        runtimes = _registry_runtimes(method_shaped)
        assert runtimes, (
            "a bound method exposing the registry must be resolved, not "
            "silently read as an empty list"
        )

    def test_an_object_shaped_registry_is_still_read(self, live_session):
        """NEGATIVE CONTROL: the fix handles both shapes, not just one."""
        from tradex_interfaces.safety import _registry_runtimes

        session, _, _ = live_session
        object_shaped = session.strategy_registry()
        assert not callable(object_shaped)
        assert _registry_runtimes(object_shaped)

    def test_none_and_non_registry_values_yield_nothing(self):
        from tradex_interfaces.safety import _registry_runtimes

        assert _registry_runtimes(None) == []
        assert _registry_runtimes(object()) == []
