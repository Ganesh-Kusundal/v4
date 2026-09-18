"""Round-trip trade derivation and performance statistics.

One derivation path for every execution mode: **fills in, trades out**.
Backtest, replay, paper and live all book their fills through the same
``domain.position_math.apply_fill`` model (parity review CRITICAL-1), so
pairing that shared fill stream FIFO reproduces the same round trips in every
mode. The results UI therefore reads one shape no matter where the run ran.

Pure computation, no I/O — the caller owns persistence.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill
from tradex_domain.market import Candle
from tradex_domain.utils import q2

from tradex_trading.analytics.probability import win_rate
from tradex_trading.analytics.reports import (
    NumericValue,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
)

#: Zero, quantized like every other money value in the codebase.
_ZERO = q2(Decimal(0))


@dataclass(frozen=True, slots=True)
class Trade:
    """One closed round trip — the row of the results-UI trade list."""

    symbol: str
    side: str  # "LONG" | "SHORT"
    quantity: Decimal
    entry_time: datetime
    entry_price: Decimal
    exit_time: datetime
    exit_price: Decimal
    gross_pnl: Decimal
    costs: Decimal
    net_pnl: Decimal
    holding_period: timedelta
    exit_reason: str = ""
    #: Maximum adverse / favourable excursion as a *price* distance from the
    #: entry, one-sided (both are >= 0). ``None`` when no bars were supplied
    #: to measure the path between entry and exit.
    mae: Decimal | None = None
    mfe: Decimal | None = None

    @property
    def is_win(self) -> bool:
        """True when the round trip made money after costs."""
        return self.net_pnl > 0

    def to_dict(self) -> dict[str, object]:
        """JSON-ready row for the API/chart (times as IST epoch seconds)."""
        return {
            "symbol": self.symbol,
            "side": self.side,
            "qty": float(self.quantity),
            "entry_time": int(self.entry_time.timestamp()),
            "entry_price": float(self.entry_price),
            "exit_time": int(self.exit_time.timestamp()),
            "exit_price": float(self.exit_price),
            "gross_pnl": float(self.gross_pnl),
            "costs": float(self.costs),
            "net_pnl": float(self.net_pnl),
            "holding_period": self.holding_period.total_seconds(),
            "exit_reason": self.exit_reason,
            "mae": None if self.mae is None else float(self.mae),
            "mfe": None if self.mfe is None else float(self.mfe),
        }


@dataclass(slots=True)
class _Lot:
    """One open entry lot awaiting its closing fill(s)."""

    signed_qty: Decimal  # > 0 long, < 0 short
    price: Decimal
    time: datetime
    cost_per_unit: Decimal


def _instrument_symbol(instrument: object) -> str:
    """Display symbol for *instrument* — matches the datalake's keying."""
    symbol = getattr(instrument, "symbol", None)
    return str(symbol) if symbol else str(getattr(instrument, "instrument_id", instrument))


def _excursions(
    bars: Mapping[str, Sequence[Candle]],
    symbol: str,
    entry_time: datetime,
    exit_time: datetime,
    entry_price: Decimal,
    is_long: bool,
) -> tuple[Decimal | None, Decimal | None]:
    """Return ``(mae, mfe)`` price distances, or ``(None, None)`` without bars.

    Walks only the bars inside ``[entry_time, exit_time]`` via binary search on
    the (assumed time-sorted) series, so this stays O(log n + bars_in_trade).
    """
    series = bars.get(symbol)
    if not series:
        return None, None
    times = [b.timestamp for b in series]
    lo = bisect_left(times, entry_time)
    hi = bisect_right(times, exit_time)
    window = series[lo:hi]
    if not window:
        return None, None
    highest = max(b.ohlc.high.value for b in window)
    lowest = min(b.ohlc.low.value for b in window)
    if is_long:
        # Long: adverse = price fell below entry, favourable = price rose above.
        return q2(max(entry_price - lowest, _ZERO)), q2(max(highest - entry_price, _ZERO))
    return q2(max(highest - entry_price, _ZERO)), q2(max(entry_price - lowest, _ZERO))


def round_trip_trades(
    fills: Iterable[Fill],
    *,
    cost_by_order: Mapping[str, Decimal] | None = None,
    reason_by_order: Mapping[str, str] | None = None,
    bars_by_symbol: Mapping[str, Sequence[Candle]] | None = None,
) -> list[Trade]:
    """Pair *fills* into closed round trips using FIFO lot matching.

    A lot opened by a fill is closed by the next opposing fills until its
    quantity is exhausted, so partial exits and scale-ins produce one ``Trade``
    per matched portion (the standard FIFO attribution). Reversals work: an
    oversized opposing fill closes every open lot and opens a fresh one with
    the remainder.

    Parameters
    ----------
    fills:
        Fills in any order; they are sorted by timestamp before matching.
    cost_by_order:
        Total cost (fees + taxes + slippage) booked per ``order_id``,
        attributed to each matched portion in proportion to its quantity.
    reason_by_order:
        Exit reason per ``order_id`` (e.g. ``"stop_loss"``, ``"target"``),
        stamped onto the trade the fill closes.
    bars_by_symbol:
        Time-sorted bars keyed by the same symbol :func:`_instrument_symbol`
        returns, used to measure MAE/MFE. Omit to leave both ``None``.

    Returns
    -------
    list[Trade]
        Closed round trips in exit order. Fills that never close stay open and
        are not reported — matching ``Position``, which is still non-zero.
    """
    costs = cost_by_order or {}
    reasons = reason_by_order or {}
    bars = bars_by_symbol or {}
    open_lots: dict[str, deque[_Lot]] = {}
    trades: list[Trade] = []

    for fill in sorted(fills, key=lambda f: f.timestamp):
        symbol = _instrument_symbol(fill.instrument)
        total_qty = abs(fill.quantity.value)
        if total_qty == 0:
            continue
        # Per-unit cost for this fill, so each matched portion carries its share.
        cost_per_unit = q2(costs.get(str(fill.order_id), _ZERO) / total_qty)
        signed = total_qty if fill.side == OrderSide.BUY else -total_qty
        price = fill.price.value

        lots = open_lots.setdefault(symbol, deque())

        while signed != 0 and lots and (lots[0].signed_qty > 0) != (signed > 0):
            lot = lots[0]
            matched = min(abs(signed), abs(lot.signed_qty))
            is_long = lot.signed_qty > 0
            # Long: bought at lot.price, sold at price. Short: sold at
            # lot.price, bought back at price.
            gross = q2((price - lot.price) * matched if is_long else (lot.price - price) * matched)
            trade_cost = q2((lot.cost_per_unit + cost_per_unit) * matched)
            mae, mfe = _excursions(bars, symbol, lot.time, fill.timestamp, lot.price, is_long)
            trades.append(
                Trade(
                    symbol=symbol,
                    side="LONG" if is_long else "SHORT",
                    quantity=matched,
                    entry_time=lot.time,
                    entry_price=lot.price,
                    exit_time=fill.timestamp,
                    exit_price=price,
                    gross_pnl=gross,
                    costs=trade_cost,
                    net_pnl=q2(gross - trade_cost),
                    holding_period=fill.timestamp - lot.time,
                    exit_reason=reasons.get(str(fill.order_id), ""),
                    mae=mae,
                    mfe=mfe,
                )
            )
            lot.signed_qty -= matched if lot.signed_qty > 0 else -matched
            signed -= matched if signed > 0 else -matched
            if lot.signed_qty == 0:
                lots.popleft()

        if signed != 0:
            lots.append(
                _Lot(
                    signed_qty=signed,
                    price=price,
                    time=fill.timestamp,
                    cost_per_unit=cost_per_unit,
                )
            )

    return trades


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
        profit_factor=(gross_profit / abs_loss) if abs_loss > 0 else 0.0,
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


__all__ = ["Statistics", "Trade", "compute_statistics", "round_trip_trades"]
