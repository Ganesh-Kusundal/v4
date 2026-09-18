# TradeX v4 — Documentation Index

Authoritative sources (always current):

- `../CLAUDE.md` — agent guide: standard interfaces, datalake facts, architecture map, do-nots
- `ARCHITECTURE.md` — system architecture
- `design/state.md` — running design-state log

## Discovery & gap analysis

- `design/2026-09-13-backtest-engine-discovery.md` — the backtest/strategy/
  screener/live-parity engine audited against the 38-section target spec:
  traced data flows, per-subsystem reuse verdicts, indicator placement audit,
  and the ten real gaps (G1–G10). Read this before proposing new engine work.
- `design/2026-09-13-frontend-integration-map.md` — the three-layer map
  (library 2.1.8 / host `frontend/` / backend routes) with the wired flows and
  the exact unwired backend capabilities. Read this before building UI.
- `design/2026-09-13-workspace-view-restore.md` — the saved-viewport restore
  defect: why a view is only valid against the series it was captured on, the
  series fingerprint that decides it, and the Playwright suite (with a negative
  control) that proves the guard fires.
- `design/2026-09-13-single-store-persistence.md` — chart state kept in one
  store (the server workspace blob), the "active layout = most recently saved"
  rule that replaces `localStorage`'s role, and the drawings coupling that made
  turning persistence off riskier than it looked.
- `design/2026-09-13-backtest-results-surface.md` — the backtest run rendered as
  chart objects (markers, position zones, equity + drawdown, statistics, SL/TP
  brackets), the `attach`-vs-`table` collision that silently killed the pane's
  fetch, and the signal→order contract that carries a strategy's declared
  protective levels all the way to a drawn line — plus what it does not enforce.
- `design/2026-09-13-trades-table.md` — the per-trade table (one row per round
  trip, gross P&L, the open lot as dashes), why rows need real elements where a
  `ChartTable` cannot name one, the pick→zone selection contract, the run-window
  defect it exposed (a live bar append re-ran the strategy over the appended
  sliver), and the two negative controls that make its E2E cover real.
- `design/2026-09-15-volume-profile-execution.md` — the intraday execution
  layer in the top-gainers app: trading each scan pick against its own
  09:15→09:50 value area (opening day-type entry, edge fades, acceptance
  breakouts, the 80%-rule re-entry), the sizing/cycle/cost model, and the
  measured attribution that says the opening regime is the only rule with
  evidence while the intraday rules and the POC/HVN targets are net losers.

## Verification & build integrity

- `design/2026-09-13-stale-artifact-verification.md` — the audit of every command
  that could pass against stale build output: fixtures captured from a library
  version nobody runs, a browser suite that could test a previous bundle, a mount
  check asserting a marker that never existed, a smoke script whose order checks
  had been failing unseen. What each now verifies, the controls that prove it
  fires, and the drift (2.1.7 → 2.1.8 defaults, footprint shape) that surfaced
  once the fixtures were regenerated from the pin.

## Specs & plans (design history, newest last)

`superpowers/specs/` and `superpowers/plans/` hold one file per designed
sub-project, named `YYYY-MM-DD-<topic>`. Key recent items:

- `2026-09-08-indicator-parity-2-1-0` — full 102-indicator parity with
  openalgo-charts 2.1.0 (differential harness, 12 ports, drift fixes)
- `2026-09-09-depth-conformance` — WS depth frames match engine MarketDepth
- `2026-09-09-ws-indicator-push` — Tier-2 subscribe seam (bar-close + tick)
- `2026-09-09-workspace-persistence` — opaque-blob chart-state CRUD
- `2026-09-09-study-strategies` — five chart-study signal strategies
- `2026-09-08-sync-facade`, `2026-09-07-*` — datalake sync and hardening

## Reviews

`reviews/` holds dated architecture/code reviews. Reviews are records of
their date — superseded verdicts live in `archive/<year-month>/`:

- `archive/2026-08/` — pre-hardening reviews (2026-08-29)
- `archive/2026-09/` — execution-state review + fix status (2026-09-01),
  both superseded by the landed hardening work

## Superseded

- `ui-zero-parity-plan.md` / `superpowers/specs/2026-09-05-ui-zero-parity-*`
  describe the deleted `frontend/`; they remain as history for the future
  frontend rebuild (frontend rebuild is the one open workstream).
