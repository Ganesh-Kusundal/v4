"""AC-9 / lane 2c — per-strategy capital and drawdown baselines survive a restart.

The audit's defect: the daily-loss and drawdown baselines lived only in
``RiskManager`` memory, so a crash-restart reset them and a tripped breaker
came back armed. These tests persist a baseline through a real SQLite file and
read it back in a *second* manager, which is the only way to show a restart
kept it.

The positions here carry a ``strategy_id`` — the AC-9 data-model field — via a
small stand-in so the per-strategy split can be exercised before that field
lands on ``Position``. ``RiskManager`` reads it with ``getattr``, which is the
same defensive path it uses for ``request.strategy_id``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Money, Price, Quantity
from tradex_execution.risk import RiskManager
from tradex_execution.risk_baseline_store import (
    SQLiteRiskBaselineStore,
    StrategyBaseline,
)
from trading.tests.risk_support import FrozenClock

_CLOCK = FrozenClock()
_INST = Equity.of("NSE", "TEST")


@dataclass(frozen=True, slots=True)
class _OwnedPosition:
    """A position the book can attribute to one strategy (AC-9 shape)."""

    strategy_id: str
    realized: Decimal
    quantity: Quantity
    mark_price: Price
    marked_at: datetime

    @property
    def instrument(self) -> Equity:
        return _INST

    @property
    def avg_price(self) -> Price:
        return Price(value=Decimal("100"))

    @property
    def realized_pnl(self) -> Money:
        return Money(amount=self.realized)

    @property
    def unrealized_pnl(self) -> Money:
        return Money(amount=Decimal("0"))


def _book(strategy_id: str, realized: Decimal, mark_at: datetime) -> list[_OwnedPosition]:
    return [
        _OwnedPosition(
            strategy_id=strategy_id,
            realized=realized,
            quantity=Quantity(value=Decimal("10")),
            mark_price=Price(value=Decimal("100")),
            marked_at=mark_at,
        )
    ]


def _buy(tag: str | None = None) -> OrderRequest:
    return OrderRequest(
        instrument=_INST,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        tag=tag,
    )


def test_store_round_trips_a_baseline(tmp_path) -> None:
    """A saved baseline reads back byte-for-byte as money, not as a float."""
    store = SQLiteRiskBaselineStore(str(tmp_path / "risk.db"))
    store.save(
        StrategyBaseline(
            strategy_id="sma_cross",
            session_date="2026-09-27",
            base_pnl=Decimal("0"),
            peak_pnl=Decimal("12345.67"),
        )
    )
    loaded = store.load("sma_cross")
    assert loaded is not None
    assert loaded.peak_pnl == Decimal("12345.67")
    assert isinstance(loaded.peak_pnl, Decimal)
    store.close()


def test_store_reopens_the_same_file(tmp_path) -> None:
    """The file, not the object, is the durable medium: a second store over the
    same path sees the first one's row."""
    path = str(tmp_path / "risk.db")
    first = SQLiteRiskBaselineStore(path)
    first.save(StrategyBaseline("alpha", "2026-09-27", Decimal("0"), Decimal("500")))
    first.close()
    second = SQLiteRiskBaselineStore(path)
    assert second.load("alpha").peak_pnl == Decimal("500")
    second.close()


def test_a_restart_keeps_the_drawdown_breaker_tripped(tmp_path) -> None:
    """The negative control that matters: a strategy that had already drawn
    down past its limit is STILL refused after a restart, because the peak
    survived. Before this the fresh manager began at a flat baseline and
    admitted the very order the breaker had stopped."""
    path = str(tmp_path / "risk.db")
    now = datetime.now(UTC)

    # Session one: the strategy peaks at +1000, then falls to 0 — a 100%
    # drawdown, which is past a 50% limit.
    store1 = SQLiteRiskBaselineStore(path)
    pnl = [Decimal("0")]
    rm1 = RiskManager(
        max_drawdown_pct=Decimal("0.5"),
        baseline_store=store1,
        positions_provider=lambda: _book("alpha", pnl[0], now),
        clock=_CLOCK,
    )
    rm1.check(_buy(tag="alpha@1.0.0"), now=now)          # baseline 0
    pnl[0] = Decimal("1000")
    rm1.check(_buy(tag="alpha@1.0.0"), now=now)          # peak 1000
    pnl[0] = Decimal("0")                                 # back to flat
    assert rm1.check(_buy(tag="alpha@1.0.0"), now=now) is False
    store1.close()

    # Session two: a fresh manager over the same file, the same day, the same
    # flat book. The peak came back, so the breaker is still armed.
    store2 = SQLiteRiskBaselineStore(path)
    pnl[0] = Decimal("0")
    rm2 = RiskManager(
        max_drawdown_pct=Decimal("0.5"),
        baseline_store=store2,
        positions_provider=lambda: _book("alpha", pnl[0], now),
        clock=_CLOCK,
    )
    assert rm2.check(_buy(tag="alpha@1.0.0"), now=now) is False, (
        "a tripped drawdown breaker must survive a restart"
    )
    store2.close()


def test_without_the_store_a_restart_forgets_the_peak(tmp_path) -> None:
    """Negative control for the test above: with no durable store the peak is
    lost exactly as it was before this lane, and the breaker comes back armed.
    This is what makes the persistence load-bearing rather than incidental."""
    now = datetime.now(UTC)
    pnl = [Decimal("0")]
    rm1 = RiskManager(
        max_drawdown_pct=Decimal("0.5"),
        positions_provider=lambda: _book("alpha", pnl[0], now),
        clock=_CLOCK,
    )
    rm1.check(_buy(tag="alpha@1.0.0"), now=now)
    pnl[0] = Decimal("1000")
    rm1.check(_buy(tag="alpha@1.0.0"), now=now)
    pnl[0] = Decimal("0")
    assert rm1.check(_buy(tag="alpha@1.0.0"), now=now) is False

    # A brand-new manager with no store has no memory of that peak.
    pnl[0] = Decimal("0")
    rm2 = RiskManager(
        max_drawdown_pct=Decimal("0.5"),
        positions_provider=lambda: _book("alpha", pnl[0], now),
    )
    assert rm2.check(_buy(tag="alpha@1.0.0"), now=now) is True


def test_a_previous_days_baseline_is_not_reused(tmp_path) -> None:
    """A row stamped with yesterday's date must not leak into today's limit.
    The day rolls forward, so an old row is dropped on load and today starts
    from the current book."""
    store = SQLiteRiskBaselineStore(str(tmp_path / "risk.db"))
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    store.save(StrategyBaseline("alpha", yesterday, Decimal("0"), Decimal("999999")))
    now = datetime.now(UTC)
    rm = RiskManager(
        max_drawdown_pct=Decimal("0.5"),
        baseline_store=store,
        positions_provider=lambda: _book("alpha", Decimal("0"), now),
        clock=_CLOCK,
    )
    # No peak was carried over, so there is no drawdown to act on.
    assert rm.check(_buy(tag="alpha@1.0.0"), now=now) is True
    store.close()


def test_two_strategies_do_not_share_a_baseline(tmp_path) -> None:
    """The point of per-strategy baselines: strategy B's peak must not arm or
    disarm strategy A's breaker. Here A peaked at 1000; B is flat all day, so
    B's own drawdown is zero and B is unaffected by A."""
    store = SQLiteRiskBaselineStore(str(tmp_path / "risk.db"))
    now = datetime.now(UTC)
    rm = RiskManager(
        max_drawdown_pct=Decimal("0.5"),
        baseline_store=store,
        positions_provider=lambda: _book("alpha", Decimal("1000"), now),
        clock=_CLOCK,
    )
    # A peaks at 1000 on its first evaluation.
    rm.check(_buy(tag="alpha@1.0.0"), now=now)
    # B owns a different book entirely; against B's own flat baseline there is
    # no drawdown, so B is admitted even though A is sitting on a peak.
    assert rm.check(_buy(tag="beta@1.0.0"), now=now) is True
    store.close()
