# Indicator parity with openalgo-charts 2.1.0 — design

Date: 2026-09-08. Approach: differential harness (option A). Target: engine 2.1.0 (fresh clone).

## 1. Goal and scope

Goal: the backend indicator surface exactly matches openalgo-charts 2.1.0. All
102 Tier-1 calcs reproducible through `POST /api/charts/indicators/compute`,
proven by a differential harness running engine calcs and backend calcs on
identical bars.

In scope: port the 12 missing indicators (standard-error-bands, ma-channel,
hull-suite, linreg-slope, net-volume, smma, t3, seasonality,
consolidation-breakout, standard-deviation, standard-error,
chaikin-volatility); drift-audit and correct the existing 90 against 2.1.0;
extend PARAM_MAP and goldens to all 102; Tier-2 conformance check of the
compute endpoint against 2.1.0 `external.ts`.

Out of scope: live `subscribe` push, new strategies, drawings persistence.
Separate specs.

## 2. Architecture

Three pieces. (1) Node dump script in the engine checkout
(`openalgo-charts/scripts/dump-indicator-parity.mjs`, output gitignored) that
imports `dist/openalgo-charts.indicators.mjs`, runs every descriptor `calc` on
`trading/tests/analytics/goldens/fixtures.json` bars with default settings,
writes `parity-102.json`. (2) Python ports in
`trading/src/tradex_trading/analytics/` following the existing family module
layout, registered under engine ids with engine input keys and plot keys.
(3) Tests: `test_golden_parity.py` extended to the dump output; mismatches
fixed until green; goldens regenerated from the dump.

## 3. Components and data flow

Dump script runs after each engine build. Python ports mirror engine
descriptors: id, inputs (key/type/default), plots (key/type/title). The audit
reuses the same harness for the existing 90; each drift becomes a fix task
with golden update. Tier-2 conformance asserts the endpoint response shape
`{id, points: [{time, ...plots}], meta}` satisfies what 2.1.0
`createTier2Indicator` consumes: time-keyed, null-padded, no NaN.

## 4. Error handling

Harness failures are index-precise (`{id}.{plot}[{i}]`). Unknown engine ids
fail loudly; unported ids live on an explicit skip list that shrinks to zero.
The existing "every backend indicator has a param mapping" test extends to
"every engine indicator has a backend port". Endpoint keeps 422 on unknown
id or bad params.

## 5. Testing

Golden parity for all 102 at 1e-9 None-aligned; new
`test_tier2_contract_2_1_0.py` for endpoint shape plus settings-key parity
against descriptor inputs; full suite stays green (baseline 2939 passed,
2 skipped). No e2e; browser proof deferred to the host rebuild.

## 6. Self-review

No placeholders. Scope is one plan. Requirement "mirror exactly" is pinned to
ids, input keys, and plot keys (display names stay backend-style). Version is
pinned to 2.1.0, not "latest".
