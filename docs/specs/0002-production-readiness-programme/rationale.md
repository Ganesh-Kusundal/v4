# 0002. Production readiness programme, rationale

Decision record for [index.md](index.md). Read by humans and by `/architect` on update. Not read
during a build.

## Context

The platform passes 4,341 tests, has a genuinely well factored execution spine, and cannot safely
size a position. Thirteen specialist reviewers found this in one pass. Three things made it
survive: a risk layer that is written, tested, and wired so that it constrains nothing; a backtest
whose numbers are wrong in the flattering direction; and a user interface that cannot place a live
order at all.

Underneath that sit three forces that decide what a plan can even be.

**The documentation cannot be trusted to say what is current.** Five architecture plans exist
under `docs/`, and the three most recent supersede the two before them. `docs/design/state.md`
calls itself the living design state, was last updated 2026-08-30, records the suite at 2,808
tests against an actual 4,341, and lists per strategy `RiskBudget` as shipped and closed when it
is dead code that no configuration path can reach. The file meant to answer "what is current" is
wrong about a control that costs money. A sixth plan would have made this worse.

**The work will be done by several agents in parallel.** That is the capacity, and it is the
constraint that should shape the document most. The characteristic failure of parallel agents is
two of them editing one file, where the second silently reverts the first. So a plan that is a
linear sequence wastes the capacity, and a plan with no wave boundaries has no entry conditions
and no gates.

**The first target is paper trading, and it runs on one machine in one process.** This inverts the
review's priority order. With paper as the target, the decision actually being made is which
strategy to promote, and that decision is read off backtest numbers that currently flatter. So
research validity leads and the live money hardening follows. Single process on one machine also
removes most of the performance programme: the measured 22,661 ticks per second ceiling and the
500 symbol socket cap are not binding when one strategy trades a few symbols, so the multi
process work the review recommended is deliberately not scheduled here.

## Options considered

### Option 1: live money first

Take the review at face value and work the money path top down: explicit sizing, non optional
risk limits, the margin model, live corporate actions, secret permissions, the reconciliation
monitor, real auth.

**Pros**:
- Shortest path to being able to trade real money, if that is genuinely soon.
- Fixes the defects that would lose money outright if a live order went wrong.

**Cons**:
- Every promotion decision in the meantime is made on numbers that are wrong in the flattering
  direction, so the whole programme is aimed at a strategy that may not deserve live capital.
- Does not fix the research layer, so promotion has no evidence to stand on.

### Option 2: balanced programme across everything

Run all thirteen review areas as parallel streams with no gate, sized to a quarter.

**Pros**:
- No area starves; every known defect eventually gets attention.
- Simple to staff, since any agent can pick any stream.

**Cons**:
- No gate means live money work can start before the research is sound, and a half built risk
  layer is worse than an absent one because it reads as protection.
- Nothing is independently shippable at the end of a quarter, so there is no point at which the
  platform is in a better state than it is now.

### Option 3: gate on research validity, consolidate to five packages, wave the rest

Waves 0 to 5 are ungated and get to a trustworthy paper setup with an executable promotion bar.
Waves 6 and 7 are gated: they exist in the plan, and they do not start until a strategy clears
the bar. Code moves from seventeen workspace members to five packages.

**Pros**:
- The leading edge produces a decision the operator can actually make, and the bar is an
  assertion rather than a judgement call.
- Every wave is independently shippable and gated, so a wave cannot half land.
- Later waves are designed rather than invented, so the live work does not need redeciding.
- Four packages matches the three real seams, so the boundary test defends a proportionate number
  of decisions.

**Cons**:
- Delays every money path hardening item by at least two waves.
- The largest single item, moving 53,800 lines of tests, sits in Wave 4 and produces a big diff
  with little behaviour change.

### Option 4: paper phase only

Cover only up to a trustworthy paper setup and the promotion report, and leave the live work to
the existing plans.

**Pros**:
- Smallest document and fastest to finish.
- No risk of building live machinery before it is earned.

**Cons**:
- Leaves five competing plans in place for the live work, which is the documentation problem this
  spec exists to fix.
- The live work then has no agreed sequence at all, and the review's findings about it are never
  sequenced.

## Rationale

Option 3 wins because it is the only one where the ordering follows from something other than
severity. Severity said "fix live money risk first", but live money risk only matters once a
strategy has been chosen, and the choosing is done by reading numbers that are currently wrong in
the direction that makes a strategy look good. Fixing the money path first would have produced a
very safe platform for a strategy that had not earned it.

The package target follows from the same reasoning applied to structure. The review measured
that `domain` has zero internal dependencies, that the boundary test is well built, and that the
real seams are pure types, the money path, and infrastructure. Seventeen workspace members mean seventeen
`pyproject.toml` files and a 795 line boundary test defending three decisions, against 11 mypy
invocations that do not even cover all of them. The existing proposal for twelve packages is more
fragmented than today, not less, so it was rejected. Crucially this is the *last* structural decision and not the
first, because the structural migration is only safe once the tests sit beside the code, and
moving 53,800 lines of tests is only worth doing once the tests mean something.

Wave boundaries rather than a linear sequence follows from the capacity. Each wave has one entry
condition and one gate, so a wave can be worked by several agents at once without a wave half
landing. Where lanes do contend, the plan says so in one advisory paragraph rather than imposing a
file ownership matrix, which is the level of ceremony that matches the risk.

The promotion bar is written into the report as an assertion rather than left to judgement,
because the specific failure mode here is a report that looks impressive and a human who promotes
on it. Making it executable means a strategy that does not clear the bar is visibly
unpromotable.

## Evidence

### Where the five superseded plans went

Nothing in the five plans is lost. Each idea is either in a wave above or is recorded here as
deliberately not scheduled.

| Source plan | Disposition |
|---|---|
| `docs/proposed-target-architecture.md` | Its package proposal (12) rejected in favour of 4, for the reasons above. Its flows (boot, live order, shutdown, backtest) are carried into Designs Flows A to E, corrected where the review found them wrong. Its 5 dependency rules are kept as the Wave 4 gate. |
| `docs/target-architecture-design.md` | Compatibility core concept absorbed into the account scoped event store in Flow E. Migration phases absorbed into Waves 3 and 4. |
| `docs/deep-refactor-plan.md` | Phases 1 and 2 (feed health, aggregation ownership) are Wave 3 lanes 3a, 3b, 3c. Phase 3 (typed envelopes) is deliberately dropped: the review confirmed the 433 line envelope module is dead code and its absence is not a gap. Phases 4, 5, 6 are Waves 1, 2 and 4. Phase 10 observability is Wave 5. |
| `docs/architecture-review.md` | Its quant requirements section is Wave 1. Its recommended target architecture is superseded by the package decision. Its test strategy is the Wave 0 coverage gate. |
| `docs/ARCHITECTURAL-AUDIT-2026-09-25.md` | 66 KB. Its findings on indicator duplication and the private `_sma` and `_atr` copies are Wave 4c. |
| `docs/specs/remediation-and-hardening.md` | Stays In Progress, unaffected. It owns the quality gate; Wave 0c extends it. |

### Where the thirteen panel review findings went

The review scored the platform 3.5 out of 10 for live capital. Of its 20 top risks:

| Wave | Risks closed |
|---|---|
| 0 | the red lint gate, and the untested benchmark that reports 38 times too low |
| 1 | 1 share sizing, invalid backtests, gross realised profit and loss, intraday only fees, cumulative average partial fills, survivorship, look aheads |
| 2 | no strategy kill switch, no strategy attribution, unwired per strategy budgets, optional risk limits |
| 3 | hourly bars 30 minutes off the session grid, the bar that never closes, no heartbeat, no resync, undetectable feed loss, the indicator push that degrades the feed, the blocking venue call on the feed thread |
| 4 | the 133 shim files, the 18 package sprawl, the real dependency ring, 1,112 lines of dead frontend, the missing complexity and branch coverage gates |
| 5 | no alerting, dead structured logging, no trace id, world readable secrets, no reconciliation monitor, seven missing runbooks |
| 6 | margin and leverage model, gross short exposure, live corporate actions, the account dimension, request signing, the API key on the root route, the cash reconciliation that compares the wrong quantities |

### Findings deliberately not scheduled

These are real, and this programme chooses not to fund them yet. They are recorded so the choice
is visible rather than forgotten.

- **Multi process and socket sharding.** Not binding at one strategy and a few symbols. Funded in
  Wave 7 only if forced.
- **The market data tape.** The highest leverage structural investment for post incident forensics
  and true backtest to live parity, and it is Wave 7. The event store is made account scoped and
  versioned in Wave 4 so this is an addition rather than a rebuild when it lands.
- **A read model and query projections.** Every interface read currently hits the in memory write
  model, which does not scale past one account. One account is the stated target, so it is not
  funded. The `persistence` package that would have held it is deleted in Wave 4.
- **Per symbol and sector concentration limits.** Real money only. Wave 6.
- **Multi account and sub account authz.** One operator, one account. Wave 6 if ever.
- **A formal pre trade risk engine with a pre open checklist.** The `RiskManager` has good
  primitives and no policy. Wave 6.

## References

**Project sources** (verifiable, in this repository):

- `docs/audit/2026-09-27-full-platform-review.md` — the 13 panel review this programme is built
  on, including the measured performance numbers and the scorecard
- `docs/proposed-target-architecture.md` · `docs/target-architecture-design.md` ·
  `docs/deep-refactor-plan.md` · `docs/architecture-review.md` ·
  `docs/ARCHITECTURAL-AUDIT-2026-09-25.md` — the five superseded plans, reconciled above
- `trading-predeploy-review.md` · `trading-predeploy-plan.md` ·
  `trading-deep-review-findings.md` — the three repository root reviews moving to `docs/audits/`
- `docs/specs/remediation-and-hardening.md` — In Progress, owns the quality gate
- `docs/specs/0001-deepen-parquet-market-history.md` — Proposed, adjacent, not superseded
- `docs/operations/metrics-catalog.md` — the alert thresholds Wave 5a implements
- `docs/ARCHITECTURE.md` · `CLAUDE.md` — the architecture map and the agent guide
- `trading/tests/regression/test_broker_timeout_unknown.py` — the fault injection pattern the
  cross workstream contract copies
- `frontend/e2e/global-setup.ts` and `frontend/scripts/artifact-stamp.mjs` — the build stamp and
  negative control pattern

**Practices and standards**:

- Strangler pattern for the package migration, which is what Waves 4a then 4b are
- Purged and embargoed cross validation, and Deflated Sharpe, for Wave 1c and 1e
- Point in time correct universes, for Wave 1c
- Idempotency keys for money operations, already implemented and extended in Flow C
- The three state ambiguous submission pattern, already implemented in Flow C step 7 and preserved
- Prometheus alerting on documented thresholds, for Wave 5a
- No web verification was run for this decision: the stack is unchanged by design, so there was no
  tool landscape to check
