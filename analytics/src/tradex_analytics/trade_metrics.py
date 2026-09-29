"""Performance statistics over round-trip trades.

The derivation lives in ``tradex_analytics._round_trips`` — one path for
every execution mode: **fills in, trades out**. Backtest, replay, paper and
live all book their fills through the same ``domain.position_math.apply_fill``
model (parity review CRITICAL-1), so pairing that shared fill stream
reproduces the same round trips in every mode, and the results UI reads one
shape no matter where the run ran.

Pure computation, no I/O — the caller owns persistence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from tradex_domain.execution import Fill, Position
from tradex_domain.utils import q2

from tradex_analytics._round_trips import _ZERO, Trade, round_trip_trades
from tradex_analytics.probability import win_rate
from tradex_analytics.reports import (
    NumericValue,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
)

# ---------------------------------------------------------------------------
# Per-strategy P&L (AC-9)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StrategyPnl:
    """Realized P&L of one strategy over a set of fills."""

    strategy_id: str | None
    realized_pnl: Decimal
    num_round_trips: int

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "realized_pnl": float(self.realized_pnl),
            "num_round_trips": self.num_round_trips,
        }


def pnl_by_strategy(
    fills: Iterable[Fill],
    *,
    cost_by_order: Mapping[str, Decimal] | None = None,
) -> dict[str | None, StrategyPnl]:
    """Group *fills* into per-strategy realized P&L.

    Fills are bucketed by ``fill.strategy_id`` FIRST and each bucket is
    matched independently by :func:`round_trip_trades`. That ordering is
    load-bearing: the lot matcher keys open lots by symbol alone, so
    matching a mixed set would let one strategy's exit close another
    strategy's entry and move P&L between the two books. Bucketing first
    means a round trip can only ever pair fills of the same strategy.

    ``strategy_id is None`` is a real bucket, not an error: an ad-hoc or
    pre-attribute order has no strategy, and it is reported under ``None``
    rather than dropped or folded into some strategy's total.

    Returns
    -------
    dict
        Keyed by ``strategy_id`` (including the ``None`` key), sorted by id
        so the output is stable.
    """
    buckets: dict[str | None, list[Fill]] = {}
    for fill in fills:
        buckets.setdefault(fill.strategy_id, []).append(fill)

    out: dict[str | None, StrategyPnl] = {}
    for sid in sorted(buckets, key=lambda s: (s is not None, s or "")):
        trades = round_trip_trades(buckets[sid], cost_by_order=cost_by_order)
        out[sid] = StrategyPnl(
            strategy_id=sid,
            realized_pnl=q2(sum((t.net_pnl for t in trades), _ZERO)),
            num_round_trips=len(trades),
        )
    return out


def positions_pnl_by_strategy(
    positions: Iterable[Position],
) -> dict[str | None, StrategyPnl]:
    """Group open/closed *positions* by their strategy, summing total P&L.

    The complement of :func:`pnl_by_strategy` for the still-open book: a
    position's ``strategy_id`` is set by the fill that opened it, so this
    answers "what does each strategy currently have at risk" without
    re-deriving attribution from the order table. The ``None`` bucket is
    real (a manual position) and is reported, never dropped.
    """
    out: dict[str | None, StrategyPnl] = {}
    for pos in positions:
        sid = pos.strategy_id
        agg = out.get(sid)
        total = pos.total_pnl.amount
        if agg is None:
            out[sid] = StrategyPnl(
                strategy_id=sid,
                realized_pnl=q2(total),
                num_round_trips=0,
            )
        else:
            out[sid] = StrategyPnl(
                strategy_id=sid,
                realized_pnl=q2(agg.realized_pnl + total),
                num_round_trips=agg.num_round_trips,
            )
    return {sid: out[sid] for sid in sorted(out, key=lambda s: (s is not None, s or ""))}


@dataclass(frozen=True, slots=True)
class Statistics:
    """The results-UI statistics block (TradingView-style strategy analysis).

    Money figures are net of ``costs`` unless the name says ``gross_``.
    ``max_drawdown`` is a negative fraction (``-0.12`` == -12%); use
    ``max_drawdown_pct`` for the percentage the UI prints.
    """

    total_trades: int
    winning_trades: int
    losing_trades: int
    net_profit: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    total_costs: Decimal
    profit_factor: float
    win_rate: float
    loss_rate: float
    average_trade: Decimal
    average_winner: Decimal
    average_loser: Decimal
    largest_win: Decimal
    largest_loss: Decimal
    expectancy: Decimal
    risk_reward: float
    max_drawdown: float
    max_drawdown_pct: float
    sharpe: float
    sortino: float
    exposure: float

    def to_dict(self) -> dict[str, float | int]:
        """JSON-ready mapping (floats — the API boundary is float-typed)."""
        return {
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "net_profit": float(self.net_profit),
            "gross_profit": float(self.gross_profit),
            "gross_loss": float(self.gross_loss),
            "total_costs": float(self.total_costs),
            "profit_factor": self.profit_factor,
            "win_rate": self.win_rate,
            "loss_rate": self.loss_rate,
            "average_trade": float(self.average_trade),
            "average_winner": float(self.average_winner),
            "average_loser": float(self.average_loser),
            "largest_win": float(self.largest_win),
            "largest_loss": float(self.largest_loss),
            "expectancy": float(self.expectancy),
            "risk_reward": self.risk_reward,
            "max_drawdown": self.max_drawdown,
            "max_drawdown_pct": self.max_drawdown_pct,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "exposure": self.exposure,
        }


def _mean(values: list[Decimal]) -> Decimal:
    """Mean of *values*, or zero when empty."""
    if not values:
        return _ZERO
    return q2(sum(values, Decimal(0)) / Decimal(len(values)))


def _equity_returns(equity_curve: Sequence[NumericValue] | None) -> list[float]:
    """Simple period-over-period returns from an equity curve."""
    if not equity_curve:
        return []
    values = [float(v) for v in equity_curve]
    returns: list[float] = []
    for previous, current in zip(values, values[1:]):
        if previous == 0:
            continue
        returns.append((current - previous) / previous)
    return returns


def compute_statistics(
    trades: Sequence[Trade],
    *,
    equity_curve: Sequence[NumericValue] | None = None,
    frequency: str = "daily",
    risk_free_rate: float = 0.0,
) -> Statistics:
    """Aggregate *trades* (and an optional equity curve) into the metrics block.

    ``exposure`` is the fraction of the window spanned by the trades that was
    spent holding a position — measured from the first entry to the last exit,
    so it needs no separate session calendar.
    """
    if not trades:
        return Statistics(
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            net_profit=_ZERO,
            gross_profit=_ZERO,
            gross_loss=_ZERO,
            total_costs=_ZERO,
            profit_factor=0.0,
            win_rate=0.0,
            loss_rate=0.0,
            average_trade=_ZERO,
            average_winner=_ZERO,
            average_loser=_ZERO,
            largest_win=_ZERO,
            largest_loss=_ZERO,
            expectancy=_ZERO,
            risk_reward=0.0,
            max_drawdown=max_drawdown(list(equity_curve or [])),
            max_drawdown_pct=max_drawdown(list(equity_curve or [])) * 100,
            sharpe=sharpe_ratio(_equity_returns(equity_curve), risk_free_rate, frequency),
            sortino=sortino_ratio(_equity_returns(equity_curve), risk_free_rate, frequency),
            exposure=0.0,
        )

    gross_pnls = [t.gross_pnl for t in trades]
    net_pnls = [t.net_pnl for t in trades]
    winners = [p for p in net_pnls if p > 0]
    losers = [p for p in net_pnls if p < 0]

    gross_profit = q2(sum((p for p in gross_pnls if p > 0), Decimal(0)))
    gross_loss = q2(sum((p for p in gross_pnls if p < 0), Decimal(0)))
    net_profit = q2(sum(net_pnls, Decimal(0)))
    total_costs = q2(sum((t.costs for t in trades), Decimal(0)))

    abs_loss = abs(gross_loss)
    average_winner = _mean(winners)
    average_loser = _mean(losers)

    # Exposure: sum of holding time over the span the trades occupied.
    first_entry = min(t.entry_time for t in trades)
    last_exit = max(t.exit_time for t in trades)
    span = (last_exit - first_entry).total_seconds()
    held = sum(t.holding_period.total_seconds() for t in trades)
    exposure = held / span if span > 0 else 0.0

    drawdown = max_drawdown(list(equity_curve or []))
    returns = _equity_returns(equity_curve)

    return Statistics(
        total_trades=len(trades),
        winning_trades=len(winners),
        losing_trades=len(losers),
        net_profit=net_profit,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        total_costs=total_costs,
        # The ratio of two quantized Decimals is a Decimal, but
        # ``Statistics.profit_factor`` is declared ``float`` and ``to_dict``
        # emits it straight into a JSON payload. Converting here makes the
        # stored value match its declared type (and the JSON-ready contract)
        # instead of parking a Decimal in a float field. The all-winner branch
        # is already a float literal, so the whole expression is float.
        profit_factor=float(gross_profit / abs_loss) if abs_loss > 0 else 0.0,
        # Reuses the one shared win-rate definition rather than re-deriving it.
        win_rate=win_rate([float(p) for p in net_pnls]),
        loss_rate=(len(losers) / len(trades)) if trades else 0.0,
        average_trade=_mean(net_pnls),
        average_winner=average_winner,
        average_loser=average_loser,
        largest_win=max(winners, default=_ZERO),
        largest_loss=min(losers, default=_ZERO),
        # Expectancy is average net P&L per trade — the per-trade edge.
        expectancy=_mean(net_pnls),
        risk_reward=(
            float(average_winner / abs(average_loser)) if average_loser != 0 else 0.0
        ),
        max_drawdown=drawdown,
        max_drawdown_pct=drawdown * 100,
        sharpe=sharpe_ratio(returns, risk_free_rate, frequency),
        sortino=sortino_ratio(returns, risk_free_rate, frequency),
        exposure=exposure,
    )


