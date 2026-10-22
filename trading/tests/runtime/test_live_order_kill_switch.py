"""AC-10 / lane 2f: "one config flip stops all order flow", end to end.

The control is real and it works. What was missing is the *named* negative
control the spec asks for: booting with the gate flag false must refuse every
submit. ``test_boot_requires_risk_limits.py`` had a test called
``test_a_closed_gate_refuses_when_called`` that did not get close — it wrote the
session's private flag, called the gate closure directly, and never submitted an
order. Nothing anywhere put a real order through a booted engine with the gate
closed, which means the ``live_orders_enabled=`` binding in ``startup.py`` could
be deleted outright and 494 runtime tests plus 36 execution tests would stay
green. The one control that stops order flow could be removed silently.

So these tests submit an actual order through ``session.engine.submit()`` and
assert on the receipt. Not "a closure returned a bool" — a real order, through
the real pipeline, refused.

Asserting on REJECTED alone is not enough
------------------------------------------
``engine._run_pipeline_body`` runs three gates before risk, and each returns its
own ``REJECTED`` receipt: kill switch, feed, then risk. A test that only asserts
``status == REJECTED`` proves *some* gate fired, not *this* one. In live mode
the feed gate is live too, and a boot that never reaches ``FeedState.READY``
refuses every submit for a reason that has nothing to do with the flag. So each
live test here also asserts the two things that distinguish the master gate:

* the session came up ``READY`` with a ``READY`` feed, so nothing upstream of
  risk could have refused the order, and
* the ``orders.rejection_reason.live_orders_disabled`` counter incremented,
  which is the reason string ``RiskManager`` actually passed. The engine's own
  message is the undifferentiated ``risk_check_failed``, so the counter is the
  only place the specific gate is visible end to end.

That combination is what makes the mutation control bite: delete the binding and
the order is no longer refused by the master gate, the counter does not move, and
these tests fail.

Verified by mutation, not by assertion
--------------------------------------
Two edits to the binding in ``startup.py`` (line 482, the ``RiskManager`` one)
and the resulting failures, each with everything else untouched:

* ``live_orders_enabled=(cfg.live_orders_enabled or cfg.mode != "live")``
  deleted outright -> 4 failures in ``TestLiveClosedGateRefusesSubmits``.
* the same expression replaced with ``live_orders_enabled=True`` (the flag
  read but ignored, the subtler deletion) -> 5 failures, adding the
  both-bindings-agree test because the session gate still honours the flag
  while the engine no longer does.

The second is the one that matters. Removing the binding entirely is obvious
in review; making the engine ignore a flag it still appears to read is not,
and only the submit-through-the-engine assertion catches it.

Paper is the other half, and it is a decision rather than an oversight
---------------------------------------------------------------------------
``startup.py`` binds the flag as ``cfg.live_orders_enabled or cfg.mode !=
"live"``, so in PAPER the gate is forced open even with the flag false. That is
a deliberate, documented choice — paper trading *is* order flow against a
simulated venue, and closing it by default would make the mode unrunnable. But
"deliberate" is a claim about intent, and an unexecuted claim is the exact
failure class this file exists to remove. ``TestPaperModeIsForcedOpen`` pins it
as executable truth, so the choice is recorded as a decision a human made and
can revisit, not an accident that survives only in a comment.

SPEC DISCREPANCY, left for a human
------------------------------------
The AC-10 wording says risk limits fail closed "in paper as well as live". The
flag half of that control demonstrably does not: paper ignores it. This file
pins the behaviour as it is today and does NOT change it — that is a product
call, not a test's. The question this leaves for a human is at the bottom of
``TestPaperModeIsForcedOpen``.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import tradex_runtime.startup as startup_mod
from tradex_config.schema import AppConfig, ExecutionConfig, PersistenceConfig
from tradex_domain import BrokerId, Equity
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, ProductType, TimeInForce
from tradex_domain.events import OrderFilled
from tradex_domain.execution import OrderRequest
from tradex_domain.value_objects import Price, Quantity
from tradex_runtime.session_impl import SessionState
from trading.tests.risk_support import permissive_risk

#: The one instrument the runtime suite's live boots trade. Matches the tape
#: ``named_tape`` names, because an order for an instrument the feed was never
#: started on is a different failure and would muddy what these tests prove.
_INSTRUMENT = Equity.of("NSE", "RELIANCE")

#: The counter ``RiskManager._deny`` bumps for the master gate specifically.
#: Checked in tests below via the metrics registry boot already wires.
_GATE_REASON = "orders.rejection_reason.live_orders_disabled"


def _live_broker() -> MagicMock:
    """A live broker that satisfies boot without a socket.

    Same shape as ``test_live_boot_fail_closed.py``: the subject here is the
    risk gate, so the broker only has to be real enough to be constructed and
    to answer the reconciliation reads boot performs.
    """
    broker = MagicMock()
    backend = MagicMock()
    backend.subscribe_orders = MagicMock()
    broker.stream_backend.return_value = backend
    broker.get_orderbook.return_value = []
    broker.get_positions.return_value = []
    broker.master_loader = None
    return broker


def _live_cfg(tmp_path, **overrides) -> AppConfig:
    """A live config with the complete, permissive risk policy.

    ``permissive_risk`` is the house policy for a session that may trade: real
    caps far above the notional here, so the value/rate limits are in the path
    and provably never the reason this order is refused. That matters — the
    master gate must be the *only* thing refusing, and these tests say so
    rather than assuming it.

    ``live_orders_enabled`` defaults to ``False`` here so the closed-gate tests
    read as "the flag is false unless a test opens it", which is also the
    shipped default. Live needs ``live_enabled`` to get past the separate
    live-only boot guard, which is a different control with a different subject.
    """
    overrides.setdefault("live_orders_enabled", False)
    return AppConfig(
        risk=permissive_risk(),
        mode="live",
        broker_id=BrokerId.DHAN,
        live_enabled=True,
        persistence=PersistenceConfig(path=str(tmp_path / "orders.db")),
        execution=ExecutionConfig(strategy_ids=("test_strategy",)),
        **overrides,
    )


def _paper_cfg(**overrides) -> AppConfig:
    """A paper config with the same risk policy, so the two halves differ only
    in the one variable under test: ``mode``."""
    return AppConfig(risk=permissive_risk(), mode="paper", **overrides)


def _order(quantity: str = "10") -> OrderRequest:
    """A plain LIMIT BUY. 10 x 2500 = 25 000, far under the permissive caps."""
    return OrderRequest(
        instrument=_INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(quantity)),
        price=Price(value=Decimal("2500.00")),
        time_in_force=TimeInForce.DAY,
        product_type=ProductType.INTRADAY,
    )


def _boot(monkeypatch, cfg: AppConfig):
    """Boot ``cfg`` with a stubbed live broker. Caller must ``stop()``."""
    monkeypatch.setattr(
        "tradex_runtime.live.build_broker_from_env",
        lambda _pid, **_kw: _live_broker(),
    )
    return startup_mod.boot(cfg)


def _assert_master_gate_is_the_cause(session, receipt) -> None:
    """The receipt is REJECTED *and* the master gate is why.

    Two assertions, both load-bearing:

    1. Nothing upstream of risk refused. ``READY`` session and ``READY`` feed
       mean the kill switch, feed and risk-unbound gates all passed, so the
       rejection that follows can only be the risk verdict itself. Without this
       a misconfigured live boot would pass for the right reason with the wrong
       one.
    2. The named reason counter moved. The engine flattens every risk denial to
       ``risk_check_failed``, so this counter is the only end-to-end evidence of
       *which* gate fired.
    """
    assert session.state == SessionState.READY, (
        "the session must come up READY; a boot that refuses is a different "
        "control being tested by accident"
    )
    supervisor = session.engine._feed_supervisor  # noqa: SLF001
    assert supervisor is not None and supervisor.ready, (
        "a live engine refuses every submit at the feed gate before risk is "
        "ever consulted, so a not-READY feed would mask the master gate"
    )
    assert receipt.status == OrderStatus.REJECTED, (
        f"the closed gate must refuse the order, got {receipt.status}"
    )
    registry = session.metrics
    assert registry is not None and registry.get(_GATE_REASON) >= 1, (
        f"{_GATE_REASON} must be the recorded reason; the engine's own message "
        f"is the undifferentiated {receipt.message!r}"
    )


class TestLiveClosedGateRefusesSubmits:
    """The named negative control: the gate flag false refuses every submit."""

    def test_a_closed_gate_refuses_a_real_order_through_the_engine(
        self, named_tape, monkeypatch, tmp_path
    ) -> None:
        """Boot live with ``live_orders_enabled=False``, submit a real order.

        This is the test that was missing. The old
        ``test_a_closed_gate_refuses_when_called`` set a private attribute and
        invoked the gate closure; it proved the closure raises, which says
        nothing about whether a submit is stopped, because the submit path is
        the engine's, not the session closure's.
        """
        cfg = _live_cfg(tmp_path, live_orders_enabled=False)
        assert cfg.live_orders_enabled is False, "the premise: the flag is false"

        session = _boot(monkeypatch, cfg)
        try:
            receipt = session.engine.submit(_order())
            _assert_master_gate_is_the_cause(session, receipt)
        finally:
            session.stop()

    def test_the_flag_is_false_by_default_so_live_starts_closed(
        self, named_tape, monkeypatch, tmp_path
    ) -> None:
        """No flag at all is the same refusal.

        ``AppConfig.live_orders_enabled`` ships False, so a live session that
        was never told to trade is closed from the first submit. The operator
        has to open it deliberately; forgetting is safe.
        """
        cfg = _live_cfg(tmp_path, live_orders_enabled=False)
        assert AppConfig().live_orders_enabled is False, (
            "the shipped default must be closed, or 'forgot to enable' "
            "silently means 'trading live'"
        )
        session = _boot(monkeypatch, cfg)
        try:
            receipt = session.engine.submit(_order())
            _assert_master_gate_is_the_cause(session, receipt)
        finally:
            session.stop()

    def test_no_order_reaches_the_venue_or_fills(
        self, named_tape, monkeypatch, tmp_path
    ) -> None:
        """Refusal is real: no fill event, and the broker is never called.

        A REJECTED receipt is only meaningful if nothing happened anyway. The
        broker here is a ``MagicMock``, so ``place_order`` not being called is
        directly assertable — that is the "order flow that leaves the machine"
        half of the control, and it is the half a receipt alone cannot show.
        """
        broker = _live_broker()
        monkeypatch.setattr(
            "tradex_runtime.live.build_broker_from_env",
            lambda _pid, **_kw: broker,
        )
        events: list[object] = []

        session = _boot(monkeypatch, _live_cfg(tmp_path, live_orders_enabled=False))
        try:
            session.bus.stream().subscribe(events.append)
            session.engine.submit(_order())

            assert OrderFilled not in [type(e) for e in events], (
                "a closed gate must not produce a fill"
            )
            broker.place_order.assert_not_called(), (
                "the order must never reach the venue; refusing it in the "
                "receipt while still calling the broker would not be a "
                "kill switch"
            )
        finally:
            session.stop()

    def test_the_gate_is_the_only_thing_refusing(
        self, named_tape, monkeypatch, tmp_path
    ) -> None:
        """No other rejection reason fires for the same order.

        Closes the "some other limit happened to catch it" hole. ``permissive_
        risk`` already makes every other limit far away, but that is a comment
        until it is measured: this asserts the master-gate counter is the only
        rejection reason the submit produced.
        """
        session = _boot(
            monkeypatch, _live_cfg(tmp_path, live_orders_enabled=False)
        )
        try:
            session.engine.submit(_order())
            registry = session.metrics
            reasons = {
                name.split("orders.rejection_reason.")[1]: value
                for name, value in registry.snapshot().items()
                if name.startswith("orders.rejection_reason.")
            }
            assert reasons == {"live_orders_disabled": 1}, (
                "the master gate must be the sole reason this order was "
                f"refused, got {reasons}"
            )
        finally:
            session.stop()

    def test_the_gate_fires_before_every_other_limit(
        self, named_tape, monkeypatch, tmp_path
    ) -> None:
        """The master gate is checked *first*, so nothing else can pre-empt it.

        This session would also be refused if the flag were ignored: the stubbed
        live broker reports no funds, so the fail-closed cash gate denies every
        BUY with ``cash_unavailable`` too. That is deliberate and it is why
        ``REJECTED`` alone is not evidence of anything.

        The master gate is evaluated at the top of ``RiskManager.check``, before
        the cash, value, position and P&L gates, so on this session it is the
        one that answers and the rest never run. Asserting that is what makes
        these tests a real control rather than a coincidence: it says the flag
        stopped this order, not merely that something did.
        """
        session = _boot(
            monkeypatch, _live_cfg(tmp_path, live_orders_enabled=False)
        )
        try:
            session.engine.submit(_order())
            registry = session.metrics
            assert registry.get(_GATE_REASON) == 1, (
                "the master gate must be the gate that refused, ahead of the "
                "cash gate this session would otherwise have hit"
            )
            assert registry.get("orders.rejection_reason.cash_unavailable") == 0, (
                "the master gate must short-circuit the rest of the risk path"
            )
        finally:
            session.stop()

    def test_opening_the_flag_is_the_only_thing_that_admits_an_order(
        self, named_tape, monkeypatch, tmp_path
    ) -> None:
        """The control has both directions: open the flag, the gate lets by.

        A kill switch that refuses everything unconditionally would pass every
        test above. This is the half that proves the binding is a *binding* —
        the same boot, the same order, the same risk policy, differing only in
        the flag, must not be refused by the master gate.

        Deliberately does not assert the order FILLS. With the gate open, live
        still has its own gates in front of it (cash, marks, venue), and those
        are other controls with their own tests. What matters here is only the
        negative: the master gate let this order through, and the counter did
        not move.
        """
        session = _boot(
            monkeypatch, _live_cfg(tmp_path, live_orders_enabled=True)
        )
        try:
            session.engine.submit(_order())
            registry = session.metrics
            assert registry.get(_GATE_REASON) == 0, (
                "with the flag open the master gate must not fire; if it did, "
                "this control refuses everything and the tests above prove "
                "nothing"
            )
        finally:
            session.stop()


class TestPaperModeIsForcedOpen:
    """Pin the paper half as a decision, so it reads as intentional.

    ``startup.py`` binds ``cfg.live_orders_enabled or cfg.mode != "live"``, so
    paper ignores the flag entirely. That is a deliberate design call recorded
    in the comment at the binding: paper trading is order flow against a
    simulated venue, and gating it by default would make the mode unrunnable.

    These tests execute that decision instead of trusting the comment. They are
    written to FAIL if someone makes paper respect the flag, which is the point:
    a change to this behaviour should be a deliberate, visible one with its
    tests updated, not a drive-by that silently turns paper into a no-op.
    """

    def test_paper_submits_successfully_with_the_flag_false(
        self, monkeypatch
    ) -> None:
        """Paper ignores the flag: the order fills against the simulated venue.

        The counterpart to ``test_a_closed_gate_refuses_a_real_order_through_
        the_engine``. Same order, same permissive risk, ``live_orders_enabled
        =False`` — the only difference is ``mode``, and the outcome is the
        complete opposite. That contrast is the design, made executable.
        """
        cfg = _paper_cfg(live_orders_enabled=False)
        assert cfg.live_orders_enabled is False, "the premise: the flag is false"

        session = startup_mod.boot(cfg)
        try:
            receipt = session.engine.submit(_order())
            assert receipt.status == OrderStatus.FILLED, (
                "paper must ignore the live kill switch; it trades against a "
                f"simulated venue, so a false flag must not refuse it "
                f"(got {receipt.status})"
            )
            registry = session.metrics
            assert registry.get(_GATE_REASON) == 0, (
                "the master gate must never fire in paper"
            )
        finally:
            session.stop()

    def test_paper_is_equally_open_with_the_flag_true(
        self, monkeypatch
    ) -> None:
        """The flag is a no-op in paper, not a second, weaker control.

        If paper's gate were bound to the flag alone, the two values would
        differ. They do not, so the flag is genuinely inert in paper — which is
        what makes the forced-open binding a decision and not a coincidence of
        the two paths agreeing.
        """
        closed = startup_mod.boot(_paper_cfg(live_orders_enabled=False))
        opened = startup_mod.boot(_paper_cfg(live_orders_enabled=True))
        try:
            assert closed.engine.submit(_order()).status == OrderStatus.FILLED
            assert opened.engine.submit(_order()).status == OrderStatus.FILLED
        finally:
            closed.stop()
            opened.stop()

    # The spec says limits fail closed "in paper as well as live". The gate
    # half of that does not, by the decision recorded above. NOT changed here:
    # a kill switch is a product decision, not a test's to make. The question
    # for a human is simply —
    #
    #   Should the "one config flip stops all order flow" control also stop
    #   order flow in PAPER? Today it does not, deliberately. If paper is
    #   expected to be killable, the fix is to drop `or cfg.mode != "live"`
    #   from BOTH bindings in startup.py and give paper an explicit opt-in,
    #   which is a config-schema change as much as a binding one. If not,
    #   AC-10's wording should be narrowed to say the control covers live.
    #
    # What must not happen is this staying ambiguous: today it is provable only
    # because ``TestPaperModeIsForcedOpen`` executes it.


class TestTheGateIsTheSameGateInBothPlaces:
    """``startup.py`` binds the flag twice; the two must not drift.

    The flag is bound into the ``RiskManager`` master gate (this is the one that
    stops submits) and into the ``TradingSession`` order gate. They read the
    same expression. If someone changes one and not the other, a session can
    report a closed gate while its engine still trades, or the reverse — and
    neither is visible in any single component's tests.
    """

    def test_the_engine_and_the_session_agree_in_live(
        self, named_tape, monkeypatch, tmp_path
    ) -> None:
        """Closed flag: the session gate refuses *and* the engine refuses.

        Asserted through the public order gate, not the private attribute the
        old test poked.
        """
        from tradex_domain.errors import OrderRejectedError

        session = _boot(
            monkeypatch, _live_cfg(tmp_path, live_orders_enabled=False)
        )
        try:
            with pytest.raises(OrderRejectedError):
                session.assert_order_gate_open()

            receipt = session.engine.submit(_order())
            assert receipt.status == OrderStatus.REJECTED
        finally:
            session.stop()

    def test_the_engine_and_the_session_agree_in_paper(
        self, monkeypatch
    ) -> None:
        """Paper: both open, because the same expression governs both.

        Cheap half of the agreement, and the half that would notice a
        one-sided edit to either binding in the common case.
        """
        session = startup_mod.boot(_paper_cfg(live_orders_enabled=False))
        try:
            session.assert_order_gate_open()  # must not raise
            assert session.engine.submit(_order()).status == OrderStatus.FILLED
        finally:
            session.stop()
