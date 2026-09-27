"""Shared risk policies for tests that boot a paper/live session.

AC-10 (lane 2f) made every enumerated risk limit required at boot: a limit left
at ``None`` refuses the session with :class:`MissingRiskLimitError`. That gate
is a control, not a defect, so every test that boots now has to *declare* a
policy. This module is the one place that policy is written down, so the
migration did not have to copy five numbers into ~46 files and drift.

Two policies, and the difference between them is the point the gate is about:

* :func:`permissive_risk` — the default. Real, deliberately huge caps on the
  three *value/rate* limits, and an explicit :data:`UNBOUNDED` opt-out on the
  two *P&L* guards. See the note on the guards below for why that is the honest
  choice for a test session rather than a fudge.
* :func:`unbounded_risk` — every limit set to the explicit ``UNBOUNDED``
  opt-out, for tests that are only about whether a session comes up at all and
  never trade through it. ``UNBOUNDED`` is a decision, which is exactly what the
  gate demands — never a raw ``-1``.

Neither factory weakens the gate: both name all five limits, so
``require_limits()`` passes because the operator (here: the test) said so, not
because the check was skipped. ``trading/tests/runtime/test_boot_requires_risk_limits.py``
still pins the refusal and is unaffected by this module.

Why the P&L guards opt out by default
-------------------------------------
``RiskManager`` evaluates ``max_daily_loss_amt`` / ``max_drawdown_pct`` only when
a positions provider is bound, and it fails *closed* on a stale mark: with those
two limits set to real numbers, a paper session with no quote-driven MTM service
denies every order that increases exposure with ``marks_not_fresh`` — starting at
the second order, once the first has opened a position. That is correct
production behaviour (you cannot price a drawdown guard without marks), but a
smoke/parity test has no mark service to satisfy it, and is not testing risk.

So :func:`permissive_risk` names the two guards as ``UNBOUNDED`` — the
documented, deliberate "no P&L guard on this session" — while keeping the value
and rate caps real and far above any notional a test places. A test that *is*
about a P&L guard overrides the field back to a real value.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tradex_config.schema import AppConfig, RiskConfig
from tradex_domain import UNBOUNDED

#: The three value/rate limits get real, round, deliberately huge values: well
#: above any notional a test places, so they are present in the risk path and
#: proven never to be the reason an order is refused.
_PERMISSIVE_VALUE_LIMITS: dict[str, Any] = {
    "max_order_value": Decimal("10000000"),
    "max_position_value": Decimal("10000000"),
    "max_orders_per_minute": 1000,
}

#: The two P&L guards opt out (see the module docstring). ``UNBOUNDED`` keeps
#: "no drawdown/loss guard here" an explicit choice rather than an omission.
_PERMISSIVE_PNL_LIMITS: dict[str, Any] = {
    "max_daily_loss_amt": UNBOUNDED,
    "max_drawdown_pct": UNBOUNDED,
}

_ALL_LIMIT_NAMES = (
    "max_order_value",
    "max_position_value",
    "max_orders_per_minute",
    "max_daily_loss_amt",
    "max_drawdown_pct",
)


def permissive_risk(**overrides: Any) -> RiskConfig:
    """A complete policy for a test session that may trade.

    Value/rate caps are real and huge; the P&L guards are explicitly
    ``UNBOUNDED``. ``overrides`` replace individual limits, so a test that *is*
    about one of them keeps its own deliberate value while inheriting complete
    ones for the rest.
    """
    base = {**_PERMISSIVE_VALUE_LIMITS, **_PERMISSIVE_PNL_LIMITS, **overrides}
    return RiskConfig(**base)


def unbounded_risk(**overrides: Any) -> RiskConfig:
    """A complete policy that opts out of every limit, on purpose.

    For tests about whether a session comes up at all. The opt-out is stated
    with the marker so "no limit" stays a choice rather than an omission.
    """
    return RiskConfig(**{name: UNBOUNDED for name in _ALL_LIMIT_NAMES}, **overrides)


def risk_section(**overrides: Any) -> dict[str, Any]:
    """The same policy in the plain-dict form ``AppConfig.from_dict`` takes.

    ``from_dict`` is strict about unknown keys, so the ``risk`` section a test
    adds to a config dict must be spelled in ``RiskConfig`` field names.
    """
    config = permissive_risk(**overrides)
    return {name: getattr(config, name) for name in _ALL_LIMIT_NAMES}


def app_config(**overrides: Any) -> AppConfig:
    """An ``AppConfig`` that already names a risk policy.

    For a test whose subject is something else and that was reaching for a bare
    ``AppConfig()`` (or bare ``boot()``, which defaults to one). Keyword
    overrides win, so a test can still set ``mode=``, ``broker_id=`` and friends.
    """
    overrides.setdefault("risk", permissive_risk())
    return AppConfig(**overrides)


__all__ = [
    "app_config",
    "permissive_risk",
    "risk_section",
    "unbounded_risk",
]
