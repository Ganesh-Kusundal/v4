"""AC-10 / lane 2f — fail closed on the enumerated limit set at boot.

Every ``RiskConfig`` limit that used to default to ``None`` is now required in
paper and live. ``None`` is *missing*; ``UNBOUNDED`` is a deliberate, documented
opt-out. These tests pin both halves of that distinction, because the whole
control is the difference between them.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_config.schema import AppConfig, MissingRiskLimitError, RiskConfig
from tradex_domain import UNBOUNDED, BrokerId
from tradex_domain.risk_budget import REQUIRED_RISK_LIMITS, is_unbounded
from tradex_runtime.startup import boot

#: A complete, deliberately generous policy: the point of these tests is the
#: *presence* of each limit, not its value.
_COMPLETE = {
    "max_order_value": Decimal("1000000"),
    "max_position_value": Decimal("5000000"),
    "max_orders_per_minute": 60,
    "max_daily_loss_amt": Decimal("30000"),
    "max_drawdown_pct": Decimal("0.10"),
}


def _complete(**overrides) -> RiskConfig:
    return RiskConfig(**{**_COMPLETE, **overrides})


# --- the enumerated required set ------------------------------------------

def test_the_required_set_is_the_five_risk_config_limits() -> None:
    """Pins the enumeration itself, so adding or dropping a required limit is a
    visible change to the contract rather than a quiet edit."""
    assert REQUIRED_RISK_LIMITS == (
        "max_order_value",
        "max_position_value",
        "max_orders_per_minute",
        "max_daily_loss_amt",
        "max_drawdown_pct",
    )
    for name in REQUIRED_RISK_LIMITS:
        assert name in RiskConfig.__dataclass_fields__


def test_a_default_config_is_missing_every_required_limit() -> None:
    """The starting point: a bare ``RiskConfig()`` names nothing."""
    assert RiskConfig().missing_limits() == REQUIRED_RISK_LIMITS


def test_a_complete_config_is_missing_nothing() -> None:
    assert _complete().missing_limits() == ()


# --- boot refuses --------------------------------------------------------

def test_paper_boot_refuses_when_a_limit_is_unset() -> None:
    """The spec's negative control: booting with a limit unset refuses boot.
    Dropping exactly one of the five is enough — there is no partial credit."""
    partial = {k: v for k, v in _COMPLETE.items() if k != "max_daily_loss_amt"}
    cfg = AppConfig(mode="paper", risk=RiskConfig(**partial))
    with pytest.raises(MissingRiskLimitError) as exc:
        boot(cfg)
    assert "max_daily_loss_amt" in str(exc.value)
    assert exc.value.missing == ("max_daily_loss_amt",)


def test_the_refusal_names_every_missing_limit_at_once() -> None:
    """An operator sees the whole gap in one refusal, not one per restart."""
    with pytest.raises(MissingRiskLimitError) as exc:
        boot(AppConfig(mode="paper"))
    assert exc.value.missing == REQUIRED_RISK_LIMITS
    for name in REQUIRED_RISK_LIMITS:
        assert name in str(exc.value)


def test_live_boot_refuses_when_a_limit_is_unset() -> None:
    """Same refusal in live, which is the mode the money-path mandate is
    ultimately about."""
    cfg = AppConfig(
        mode="live",
        broker_id=BrokerId.DHAN,
        live_enabled=True,
        risk=RiskConfig(max_order_value=Decimal("1000")),
    )
    with pytest.raises(MissingRiskLimitError):
        boot(cfg)


def test_backtest_boot_is_not_gated() -> None:
    """Negative control for the refusals above: the gate is scoped to sessions
    that can be stopped at a venue. Backtest trades recorded data and boots on
    a bare config, so the gate is not merely broken for it."""
    session = boot(AppConfig(mode="backtest"))
    try:
        assert session.state.value == "READY"
    finally:
        session.stop()


# --- the explicit unbounded opt-out --------------------------------------

def test_unbounded_is_an_explicit_opt_out_not_a_missing_limit() -> None:
    """The documented escape hatch: naming ``UNBOUNDED`` is a decision, so it
    satisfies the gate."""
    risk = _complete(max_order_value=UNBOUNDED, max_drawdown_pct=UNBOUNDED)
    assert risk.missing_limits() == ()
    assert is_unbounded(UNBOUNDED) is True
    assert is_unbounded(None) is False


def test_a_fully_unbounded_config_still_boots() -> None:
    """An operator who genuinely wants every limit open says so five times and
    the session starts — fail closed is not the same as refuse to run."""
    unbounded = RiskConfig(
        max_order_value=UNBOUNDED,
        max_position_value=UNBOUNDED,
        max_orders_per_minute=UNBOUNDED,
        max_daily_loss_amt=UNBOUNDED,
        max_drawdown_pct=UNBOUNDED,
    )
    session = boot(AppConfig(mode="paper", risk=unbounded, live_orders_enabled=True))
    try:
        assert session.state.value == "READY"
    finally:
        session.stop()


def test_the_unbounded_opt_out_really_is_unbounded() -> None:
    """The opt-out must mean *no limit*, not the number -1.

    Handing ``UNBOUNDED`` to the risk gate as a number would be worse than
    useless: a notional cap of -1 denies every order, and a daily-loss cap of
    -1 trips the loss guard on any gain. So ``UNBOUNDED`` is translated into the
    gate's own "no limit" sentinel at the config boundary, and the caps it
    produces are genuinely absent."""
    unbounded = RiskConfig(
        max_order_value=UNBOUNDED,
        max_position_value=UNBOUNDED,
        max_orders_per_minute=UNBOUNDED,
        max_daily_loss_amt=UNBOUNDED,
        max_drawdown_pct=UNBOUNDED,
    )
    caps = unbounded.effective_limits()
    assert caps == {
        "max_order_value": None,
        "max_position_value": None,
        "max_orders_per_minute": None,
        "max_daily_loss_amt": None,
        "max_drawdown_pct": None,
    }


def test_named_limits_pass_through_unchanged() -> None:
    """Negative control for the translation above: a real limit is never
    rewritten to None, or the gate would be silently switched off."""
    assert _complete().effective_limits() == _COMPLETE


def test_an_unbounded_session_actually_admits_an_order() -> None:
    """End to end: the opt-out produces a session whose order gate is open, not
    one that denies everything because a -1 reached the notional cap."""
    unbounded = RiskConfig(
        max_order_value=UNBOUNDED,
        max_position_value=UNBOUNDED,
        max_orders_per_minute=UNBOUNDED,
        max_daily_loss_amt=UNBOUNDED,
        max_drawdown_pct=UNBOUNDED,
    )
    session = boot(AppConfig(mode="paper", risk=unbounded, live_orders_enabled=True))
    try:
        risk = session.engine._risk
        assert risk._max_order_value is None, (
            "UNBOUNDED must not reach the gate as a number"
        )
    finally:
        session.stop()


def test_none_is_never_read_as_unbounded() -> None:
    """The defect this control exists to remove: ``None`` used to mean "no
    limit" and boot happily. It now means "unset", and unset refuses."""
    assert is_unbounded(None) is False
    with pytest.raises(MissingRiskLimitError):
        boot(AppConfig(mode="paper", risk=RiskConfig(max_order_value=UNBOUNDED)))


# --- the order gate is bound to the config flag ---------------------------

def test_the_gate_flag_is_false_by_default_so_live_starts_closed() -> None:
    """``AppConfig.live_orders_enabled`` is False out of the box, so a live
    session comes up refusing submits until the operator opens it."""
    assert AppConfig().live_orders_enabled is False


def test_boot_builds_the_session_order_gate() -> None:
    """``_make_order_gate()`` used to be dead code — nothing called it. Boot now
    builds it and keeps it on the session."""
    session = boot(
        AppConfig(mode="paper", risk=_complete(), live_orders_enabled=True)
    )
    try:
        assert callable(session.order_gate)
        session.order_gate()  # open gate: does not raise
    finally:
        session.stop()


def test_a_closed_gate_refuses_when_called() -> None:
    """The gate is a real control, not a closure over a constant: built from the
    session's flag, it raises when that flag is false."""
    session = boot(
        AppConfig(mode="paper", risk=_complete(), live_orders_enabled=True)
    )
    try:
        session._live_orders_enabled = False
        gate = session._make_order_gate()
        with pytest.raises(Exception) as exc:
            gate()
        assert "gate disabled" in str(exc.value)
    finally:
        session.stop()
