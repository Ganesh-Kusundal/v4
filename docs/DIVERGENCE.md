# Backtest / paper / live divergences

Ledger of known divergences between backtest, paper and live execution modes.
Every row is an accepted (not yet eliminated) difference. Rows are removed
only when the divergence is deleted or unified, or when a parity test pins it.

| Divergence | Modes affected | Owner | Expiry | Status (tests) |
|---|---|---|---|---|
| Indicator path fork — paper/live boot with `analytics=None` and strategies fall back to hand-rolled `_closes` | paper/live vs backtest | TBD (user) | 90 days from 2026-11-06 | xfail (Task 25 / T2, A3) |
| Traded-bar freeze — `SessionBarAggregator.mark_filled()` still has no production caller, so a filled bar is not protected from a late revision | all modes | TBD (user) | 90 days from 2026-11-06 | unit-covered, unwired (T1 remainder, A4) |
| Wall-clock leaks — `PaperBroker._default_clock`, `PaperBookFillSource` default `clock=None`, `TradingSession.paper()` passes nothing | paper vs backtest/live | TBD (user) | 90 days from 2026-11-06 | no xfail yet (Wave 2) |
| Pending expiry — backtest `0` (immortal) vs paper/live `pending_max_age_bars=3` | backtest vs paper/live | TBD (user) | 90 days from 2026-11-06 | no xfail yet (Wave 2) |
| Cash gating asymmetry — backtest `allow_negative_cash=(risk_manager is None)` vs paper/live `fail_closed_cash=True` | backtest vs paper/live | TBD (user) | 90 days from 2026-11-06 | no xfail yet (Wave 2) |
| Sizer equity source — fixed `initial_capital` vs live broker cash (8,333 vs 7,916 shares) | backtest vs live | TBD (user) | 90 days from 2026-11-06 | accepted (D-6); now wired at `runtime/startup.py` |
| Timezone mixing — live fills tz-aware IST vs backtest UTC/naive | all modes | TBD (user) | 90 days from 2026-11-06 | no xfail yet (Wave 2) |
| Costs — `net fees` in backtest only if caller passes `FeeCalculator`; paper/live via `fees_enabled` | all modes | TBD (user) | 90 days from 2026-11-06 | no xfail yet (Wave 2) |
| Fill timestamps — `FillModel.fill_timestamp` falls back to `now()`; `LiveFillBridge` stamps `datetime.now(UTC)` | all modes | TBD (user) | 90 days from 2026-11-06 | no xfail yet (Wave 2) |
| `ReplayController` vs backtest — replay feeds charts and never trades; no negative invariant pinned | replay vs live | TBD (user) | 90 days from 2026-11-06 | contract (Task 26 / T3) |
| Session state vs wall clock — paper refuses fills outside 09:15–15:30 IST; backtest books historical bars without gate | paper vs backtest | TBD (user) | 90 days from 2026-11-06 | diagnostic (no xfail yet) |

Resolved and removed: `bar` final-close at 15:30 (A5) — `SessionCloseFlush`
(`runtime/src/tradex_runtime/session_close.py`) is the 15:30 caller and the
parity test pins delivery; late-tick revisions are delivered as `BarRevised`
(T1, A4).

Source: `docs/audit/2026-11-06-principal-architecture-review.md` §4.3.
Gap rows carried forward from the plan: B9 (`/api/charts/account` deprecation) and B11 (`stream.py` duplicate `_replay_error`) — moved to Wave 2.