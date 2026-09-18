# Volume-profile execution layer — trade plan, rules and measured attribution

The Streamlit top-gainers app (`apps/top_gainers/app.py`) can trade its 09:45
scanner picks intraday on a **shared simulated account**. This supersedes the
UT Bot execution layer (`2026-09-14-ut-bot-execution.md`, deleted with the
code): the same sizing/cycle/cost scaffolding, but the signals come from the
**volume profile** the app was already computing, not from an ATR flip engine.

Everything below runs inside the app — no broker, no backend, no infra change.

## 1. The levels (the only inputs)

Per pick, from **1m bars strictly before 09:50** (`_vp_profile`):

| level | source |
|---|---|
| `VAL` / `VAH` | the 70% value area, expanded outward from the POC the TradingView way (`_volume_profile`) |
| `POC` | the single highest-volume price bin |
| `HVN` / `LVN` | neighbourhood peaks ≥1.2× mean volume / troughs ≤0.5×, collapsed and top-3 by prominence |

Nothing after 09:50 enters the level set, so every level is knowable at the
entry candle. Bins span the pre-window price range; `buf` (the stop buffer) is
`VP_BUF_PCT` of the value-area **width**, so stops scale with how wide the
morning's acceptance was rather than with a fixed tick count.

## 2. Regime and the rules

`_vp_regime` reads the **09:50 open** against the value area: `above` (open >
VAH), `below` (open < VAL) or `inside`. That decides the day type.

| setup | gate | trigger | side | stop | target |
|---|---|---|---|---|---|
| `open` | `vp_open` + regime ≠ inside | the 09:50 candle's **open** | long above VAH, short below VAL | back at the edge ∓ `buf` | none (rides to 15:15) |
| `re-entry` | `vp_reentry` + regime ≠ inside | close back **inside** value | against the breach | the edge + `buf` | the far edge |
| `fade` | `vp_fade` | bar tags VAH/VAL and closes back inside | against the tag | the edge + `buf` | the POC |
| `break` | `vp_break` | `vp_confirm` consecutive closes **beyond** an edge | with the move | the edge − `buf` | next HVN beyond the edge (if the target mode asks) |

Standard readings behind them: an open **outside** value is an imbalance, so
the day-type side is taken and the edge is the invalidation; the edges are the
profile's most-watched support/resistance, so a tag that closes back inside is
a fade to the POC; **acceptance** (consecutive closes beyond an edge) is the
market agreeing on a new price, so it is traded with; and an outside open that
comes back **inside** value is the "80% rule" trade across the area.

`Target` mode picks where winners leave: **EOD 15:15 only** (no target),
**Value area** (POC on a fade, far edge on a re-entry, nothing on a break) or
**Next high-volume node**.

## 3. Execution, sizing and cycle

- One position per symbol, no pyramiding, `gap_bars = 2` cooldown after any exit.
- Fills at the signal bar's **close**; the 09:50 `open` entry fills at that
  candle's **open**.
- Stop is checked **before** the target, so a bar spanning both is scored a
  loss (conservative); neither is checked on the entry bar.
- Everything is flat at **15:15**; no entry may start at/after it.
- `qty = equity × risk% ÷ |entry − stop|`, whole shares, capped by an equal
  per-pick budget and the total notional cap `equity × alloc% × leverage`
  (95% × 5× = 4.75×). Equity compounds across trades in the session.
- Costs (`VP_COST_BPS`, default 5 bps) are charged on **both** the entry and
  the exit notional. Invariant: `equity_end == capital + Σ(pnl)` (asserted in
  tests; a per-trade P&L that omitted the entry side while equity deducted it
  was a real bug found here).

## 4. Measured attribution — and the honest answer

`research/vp_strategy_backtest.py` runs **this same simulator** (imported from
the app) over the lake. 15 sessions × k=3 movers, 5m bars, ₹1cr, 2% risk,
4.75×, **net of 5 bps/side**:

| arm (as measured) | trades | win % | gross | **net @5bps** | turnover |
|---|---|---|---|---|---|
| **opening day-type entry only** | 12 | 42% | +15.9% | **+14.1%** | 18 cr |
| all rules, `Target = EOD only` | 93 | 30% | +16.5% | **+2.5%** | 140 cr |
| all rules, `Target = Value area` | 118 | 31% | +8.5% | −8.3% | 169 cr |
| all rules, `Target = Next HVN` | 181 | 23% | −51.2% | −73.1% | 229 cr |
| edge fades only | 80 | 39% | +1.6% | −11.1% | 127 cr |
| acceptance breakouts only | 126 | 21% | −47.7% | −62.9% | 159 cr |
| re-entry (80% rule) only | 22 | 14% | −1.6% | −4.0% | 25 cr |

Read plainly:

1. **The 09:50 opening day-type entry is the only rule with evidence.** It made
   +14.1% net on 12 trades and 18 cr of turnover — a tenth of the churn of the
   full rule set, and better than all of it.
2. **The intraday rules are net losers at this cost level.** Fades, breakouts
   and re-entries all cost more in turnover than they earn; the acceptance
   breakout is the worst (21% win, −63%).
3. **Profit targets hurt.** POC/HVN exits turned +2.5% into −8% to −73%
   (a target just above the edge gives a tiny reward against a full-width stop).
4. The 3-session sanity pass showed the same shape, so this is not one day.

Caveats that matter: 15 sessions is a **small sample**, and 12 opening trades
is thinner still; the scan itself was separately shown to carry **no
pre-09:45 directional edge** (see the app's scanner caption and
`research/scan_0945_forward.py`), so the opening-regime result is the one piece
worth more study, not a validated system. `vp_open`/`vp_fade`/`vp_break`/
`vp_reentry` are toggleable precisely so the app can be used as an attribution
lab rather than a tuned strategy.

## 5. Surface

- Sidebar: `Trade the scan picks (sim)`, `Opening position at 09:50`,
  `Fade rejected value-area edges`, `Trade acceptance breakouts`,
  `Outside-open re-entry (80% rule)`, `Stop buffer (% of VA width)`,
  `Acceptance closes`, `Target`, `Signal timeframe`, `Capital`,
  `Risk per trade`, `Cost per side (bps)`.
- Main: round-trip count and equity, six metrics (net win rate, net profit
  factor, net P&L against gross and cost, long/short, turnover, exits
  stop/target/EOD), the trades table with a `setup` column, a **By setup**
  table, the equity cycle, and a **Per stock** table (regime, location vs
  value, opening side + stop and risk %, POC/VAH/VAL, HVN/LVN counts, trades,
  setups, win %, P&L, exit mix) with one mini profile chart per pick.
- Chart: for the selected pick, VAH/POC/VAL guides over the session, entries
  labelled `side+setup` (`LO` = long opening, `SF` fade, `SB` break, `SR`
  re-entry) with the fill time, exits labelled with that round-trip's net P&L.
- Scanner exclusivity is preserved: the section only exists with the 09:45
  scanner on.
