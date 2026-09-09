# TradeX v4 — Documentation Index

Authoritative sources (always current):

- `../CLAUDE.md` — agent guide: standard interfaces, datalake facts, architecture map, do-nots
- `ARCHITECTURE.md` — system architecture
- `design/state.md` — running design-state log

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
