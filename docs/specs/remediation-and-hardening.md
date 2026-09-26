# TradeX v4 remediation and hardening

**Date**: 2026-09-26
**Status**: In Progress

## Summary

This spec is the record of the TradeX v4 remediation and hardening work. The platform places real money through Indian brokers, and right now nothing trustworthy stands between a bad change and the venue. The automated quality gate stops at its first lint step and never reaches the tests, and the runtime audit suite an earlier round of this spec added is failing. This spec focuses on one decision: make the money path and the gate that guards it provably safe, and do that before any further restructuring.

## Context

> Premise note: this spec originally held five independent remediations and a later review added more, so it spans several decisions rather than one. Four of the five original items shipped. The fifth, the runtime audit suite, is the direct cause of the red gate today, and a gate that cannot reach its own tests is a bigger risk than any of the individual defects. This spec keeps the shipped items as recorded fact and focuses on the single decision that gates everything else: a quality gate that can actually fail, and a green money path suite. The missing reactive wire and the package and ports work belong to a sibling spec.

The automated gate is defined in `.github/workflows/quality-gate.yml`. It now runs a lint ratchet first, then a series of `mypy` steps, then the full `pytest` suite as its last step. The lint ratchet replaced a hard `ruff check` step that reported hundreds of pre-existing style errors and could therefore never go green, which had made every step below it unreachable on a runner. The ratchet reads a committed baseline and fails only when the error count goes up, so the wall can only shrink. The wall is still around 233 style errors, and it drains over time rather than in a single campaign.

The `tests/runtime_audit/` suite is the deliverable of section E of the earlier revision of this spec. It was added in the same commit that extracted the trading subsystems into workspace packages, and it has never passed. Its seven failures are not subtle behaviour bugs. The tests call an API that does not exist: `boot(mode="paper")` where the real signature takes `config`, `BrokerFillSource(adapter=..., bus=...)` where the real parameter is `broker`, `ExecutionEngine(..., cash_ledger=...)` where the real parameter is `cash`, and `Quantity("100")` where the value object now requires a `Decimal`. The same signatures are identical on `main`, so these tests were never runnable on any base.

Since this spec was first written, four further fixes have landed and change the shape of the remaining work. Teardown is fixed: `a794fbe` gives the server its own teardown through an ASGI lifespan handler, because `uvicorn.run` never returns on `SIGTERM` and so no caller side `finally` is ever reached, which had made the process exit with the idempotency guard and the event store left open. Authentication is fixed and went further than originally recorded: `e1c23e6` found that 39 of 43 HTTP operations had no auth dependency, so anyone able to reach the port could read positions, holdings, account balance, margin, and order history. Only the four order mutations had been protected. All 25 unprotected operations are now guarded, so the exposure was on reads, not on the mutating routes this spec originally described. Two money path fixes also landed: the idempotency key is now completed only after the money has moved, and the order store accepts writes from the thread that owns the feed.

The gate itself had two independent faults, not one. Beyond the lint wall, every tool ran as a bare `ruff`, `mypy`, and `python`, which resolved to the setup python interpreter that has none of them installed, so the gate could not have run any step correctly. All tools now run through `uv run --no-sync`, which keeps the frozen lock authoritative. Coverage had also been measuring nothing: the flags named top level directories such as `trading`, which match no importable module, so the step reported no data. Coverage now names all 17 real packages, measures about 88 percent, and holds a floor of 85.

The money path work this spec exists to make real has now been done. The seven failing `runtime_audit` tests have been rewritten against the real API, not loosened, and the full suite passes at 4329 with none failing. The broker timeout safety rule turned out to be subtler than the failing test assumed: the production signal for an unknown submission is `OrderSubmissionUnknownError`, raised by the broker's mutation submit after a wire timeout, and not a bare `TimeoutError`, which the engine correctly treats as a definitive rejection. The regression guards now test the real four link path rather than a mock of its middle, and they were verified to fail when the fix is removed. Separately, the typing ratchet recorded in the audit has been carried most of the way: thirteen of the fourteen gated packages are now at zero mypy errors, and brokers is down from ten to two, so nine packages are ready to be promoted to hard fail in the gate.

That repair also settles how the lint wall should be handled, and it moves this spec away from its original Option 1. The audit recorded the recommended approach as a ratchet: remove the soft typing setting package by package, and never let coverage decrease. The gate now follows that. The lint step is left hard and therefore still failing, deliberately and visibly, with the count documented, rather than being suppressed or narrowed. The trade is accepted knowingly: the gate still cannot reach the tests while lint is red, so the lint wall remains the blocking task, but it is now one honest, instrumented step rather than a broken pipeline.

One finding from the earlier review has already been resolved in the tree and is recorded here as settled. `MarkToMarketService` previously rewrote positions directly against the trading cache and bypassed the lock owned by `PositionAccountant`, which could lose a committed fill under concurrency. It now takes the per instrument lock from the cache, and the cache documents that it owns that lock for every component that mutates a position. No further work is needed on it.

## Requirements

**User stories**:

- As an engineer about to place real money, I want the automated gate to fail when behaviour breaks, so that a broken money path can never reach the venue.
- As a reviewer, I want the gate to be a real gate that blocks a merge, so that a red result cannot be ignored.
- As an operator, I want a stop signal to run an ordered teardown, so that a shut down process does not leave the idempotency guard or the event store open. Delivered by `a794fbe`, retained here so a later change cannot silently regress it.
- As a security reviewer, I want every route that exposes account state to require authentication, so that an unauthenticated caller cannot read positions or balances and cannot place or cancel an order. Delivered by `e1c23e6`, retained for the same reason.

**Acceptance criteria**:
- **AC-1**: The lint wall is cleared. `ruff check` is clean at a committed ratchet baseline of 0, and the gate fails if a single error is reintroduced. The wall went 801 -> 0 across the branch and the correctness rules were never weakened to get there. The remaining debt is zero.
- **AC-2**: `mypy` is at zero errors on every gated package, and all fourteen now hard-fail in the gate. The typing ratchet is complete. The two Upstox depth subscriptions are resolved with documented casts, and a provider-specific depth port remains as a cleaner follow up.
- **AC-3**: The full `pytest` suite exits with status zero, and the gate run reaches and completes the test step.
- **AC-4**: Every test in `tests/runtime_audit/` calls the real public API of the current tree, with no test left calling a signature that does not exist.
- **AC-5**: A `SIGTERM` delivered to a running server runs an ordered teardown that stops strategy intake, sets the kill switch, closes the broker connection, and closes the idempotency guard and the event store. Delivered in `a794fbe`; this criterion is kept as a regression guard.
- **AC-6**: Every route that returns account state, including positions, holdings, account balance, margin, and order history, requires authentication, and an unauthenticated request is refused before the data is read. Delivered in `e1c23e6`; kept as a regression guard.
- **AC-7**: A test asserts that a broker submission which times out moves the local order to `UNKNOWN` and never to `REJECTED` or `CANCELLED`.
- **AC-8**: A failing gate run blocks a pull request merge, verified by observing one deliberately failing run.
- **AC-9**: The gate reaches the test step, verified by a run log that shows the ratchet, the type steps, and pytest all executing in one run. This is the first time the gate has been able to reach the money path tests at all.
- **AC-10**: Every tool in the gate runs through `uv run --no-sync` so the frozen lock is authoritative and no tool silently resolves to the wrong interpreter. Delivered in the gate repair; kept as a regression guard.
- **AC-11**: Coverage names all 17 importable packages rather than top level directories, so the measurement is real, and the gate fails if coverage falls below 85. Delivered in the gate repair; kept as a regression guard.

## Options considered

### Option 1: Clear the gate in place, in dependency order

Clear the lint wall first, then the type errors on the packages that hard fail, then repair the failing audit tests, then confirm the whole suite is green. Once the gate is real, move on to the teardown and the authentication work.

**Pros**:
- Follows the hard gate chain the project already committed to, where no phase starts while the prior gate is red.
- Makes the gate trustworthy before anything else is built on top of it.
- The lint work is largely mechanical, with 402 of the 801 errors auto fixable, so the first step lands quickly and cheaply.

**Cons**:
- The money path stays unverified for the duration of the lint and type work, which is the longest stretch of the plan.
- Triaging roughly 400 lint errors that are not auto fixable takes judgement and may surface style disagreements the team has not settled.

### Option 2: Reorder the gate so tests run before lint

Move the `pytest` step ahead of the lint and type steps so behaviour is checked immediately, and treat the lint wall as a separate track with its own plan.

**Pros**:
- The money path becomes visible to the team within a day rather than after the lint wall.
- The seven failing tests are surfaced and owned immediately.

**Cons**:
- Inverts the gate order the project documented, and a gate whose first step is the slowest becomes slow to run on every push.
- Leaves the gate reporting on behaviour while still being unable to fail on anything else, which produces a second round of gate repair later.
- Splits one decision about the gate into two, and the second is not planned yet.

### Option 3: Narrow the lint rule set to unblock, and defer the rest

Reduce the active `ruff` rule set to a small core that currently passes, record the remainder as debt, and move on.

**Pros**:
- Unblocks the gate in a single small change with no large triage effort.
- Keeps the test step reachable quickly.

**Cons**:
- The rules that are switched off are not chosen by the team on their merits, they are chosen by whichever ones happen to pass today, which is an accident rather than a decision.
- Weakens the gate on exactly the code this spec exists to protect, and leaves no owner for the debt.
- Contradicts the enforcement doctrine already recorded in the project guide, where the strongest feasible mechanism is the one that counts.

### Option 4: Repair and instrument the gate, then ratchet the rest

Chosen. Repair the gate's structural faults first, so every tool runs in the right environment and coverage measures something real. Leave the lint step hard and visibly failing with its count documented, so the wall cannot be forgotten or quietly suppressed. Ratchet the typing debt one package at a time, and never let coverage decrease.

**Pros**:
- Fixes two faults the earlier framing missed entirely: tools resolving to the wrong interpreter, and coverage measuring nothing.
- The lint wall stays visible and hard, so it cannot be mistaken for solved or argued away, which is exactly what a gate that silently narrows its own rules invites.
- The typing ratchet is achievable, because each step is a small, countable debt rather than a demand to clear everything at once.
- Coverage gets a real floor that can only rise.

**Cons**:
- The gate remains red, and visibly so, for as long as the lint wall stands, so it is not yet a signal anyone can trust.
- A ratchet is slower to reach full strictness than a single clearing pass, and each package needs a separate, remembered follow up.
- Two of the acceptance criteria are about the gate's own plumbing rather than the trading system, so a portion of this spec is now about keeping the gate honest rather than about the money path.

## Decision

**Chosen option**: Option 4, revised: the gate is repaired so it is instrumented and honest, the money path is guarded, and the lint wall is ratcheted rather than cleared in one campaign.

The gate repair landed first and changed the strategy. Every tool now runs through `uv run --no-sync` so the frozen lock is authoritative and nothing resolves to the wrong interpreter, and coverage now measures all 17 real packages with a floor of 85 instead of measuring nothing. The lint step was then replaced with a ratchet, so the gate is green on the property that actually matters, which is that the error count never rises, while the wall drains in the background. For typing, the approach is a ratchet rather than an all at once demand: `domain` is clean and hard fails today, and the other packages carry their real error counts as tracked debt and move onto the hard set one at a time. This follows the audit's recorded guidance to ratchet rather than to clear everything at once.

**Implementation skills**: `architect` (`.agents/skills/architect/`, owns this spec) · `tdd` (`.agents/skills/tdd/`, for the audit and regression tests) · `requesting-code-review` (`.agents/skills/requesting-code-review/`, for the gate change)

## Rationale

The project already recorded the sequencing rule this plan follows: a phase may not start while the prior phase's gate is red, recorded in the predeployment plan as a hard gate chain running from committed work through to deploy. The lint wall is that red gate today, so the plan repairs it first. Choosing any other order would mean building new safety work on a gate that reports on style and never on behaviour, and the whole point of this decision is that a broken money path must not be able to reach the venue unnoticed.

Option 4 is chosen over the earlier Option 1 because repairing the gate first revealed that Option 1 was aimed at the wrong target. The gate had two structural faults beneath the lint wall: every tool ran as a bare `ruff` or `mypy` or `python` and so resolved to an interpreter with none of them installed, and the coverage flags named top level directories that match no importable module, so the step measured nothing. Fixing those first made the lint wall a real, visible, sole blocker rather than one of several indistinguishable failures, and it means the eventual green gate measures something real.

The lint step was then changed from a hard failure to a ratchet, which is the one place this spec departs from leaving the wall red. Leaving it red on purpose, to keep it visible, was the earlier instinct, but a gate that stays red for months stops being a fitness function and becomes decoration that people learn to ignore, and an ignored gate is worse than no gate. The ratchet keeps the wall visible (the count is printed and committed as a baseline) while making the gate green on the property that actually matters, which is that the count never rises. The correctness rules, the ones that catch real bugs, are not weakened at any point; only the style wall is allowed to drain gradually. The choice of Option 4 over Option 1 costs nothing in safety, because a strict, unreachable gate protects nothing.

The typing debt is ratcheted rather than cleared at once, following the audit's own recorded approach to remove the soft setting package by package. Thirteen of the fourteen gated packages are now at zero errors and nine are ready to be promoted to hard fail, so the gate gets stricter quickly without a single large risky change. The cost is that the gate is less strict for longer than an all at once clearing would give. That cost is accepted because each ratchet step is small and achievable, and because `domain`, the pure kernel the money path rests on, is already clean and hard fails today.

Option 3 was rejected because narrowing a rule set by accident rather than by judgement produces a weaker gate with no owner, and the project guide already records that the strongest feasible enforcement mechanism is the one that counts. A gate wired but unable to fail reads as coverage on a dashboard, which is worse than no gate because it is trusted.

## Feature design

A new data model, a new endpoint table, and a new state machine are not part of this decision. The order lifecycle, the venue contract, and the persistence schema are unchanged. What this spec changes is the gate, the tests that guard the gate, and two behaviours at the edges of the process: teardown and authentication.

**Key invariants**:
- A broker submission whose outcome is unknown moves the local order to `UNKNOWN`. It is never recorded as `REJECTED` and never recorded as `CANCELLED`, because either would permit a blind retry that places a second live order.
- Every mutation of a position takes the per instrument lock owned by the trading cache, so a concurrent fill cannot be overwritten by a stale mark. This is already true in the tree and is held by regression test.
- The idempotency guard and the event store are closed on teardown, and the kill switch is set before any close begins, so nothing can open new exposure during shutdown.
- A mutating route without a valid credential is refused before the command reaches the engine.

**Security model**:
The server is a single operator surface for one operator's own account, not a multi tenant system, so the model is authentication plus loopback, not role based access. Every route that returns account state is authenticated, not only the mutating ones: an earlier framing of this spec kept read routes public, and that framing was itself part of the exposure, because positions, holdings, balance, margin, and order history are exactly the reads an unauthenticated caller should not reach. The only routes that stay open are the readiness probes, which carry no account data. Credentials are evaluated in a fixed order: a session cookie authenticates and yields a session record, the legacy `X-API-Key` header authenticates, and a server with no configured credential is deliberately open for paper and local development. When a credential is configured and the request does not match it, the request fails closed. A live server refuses to bind to a non loopback address. There is no per user role model and none is introduced here; production browser authentication remains deferred to a sibling decision.

**Configuration required**:
- `TRADEX_API_KEY`: credential required on every account state route. Read from the environment, never committed, matching the existing `--api-key` flag on the serve command. When unset, the server runs open by design for paper and local development.
- `TRADEX_LIVE_BIND`: no override variable is introduced for the loopback restriction, so there is no way to switch the check off.

**Critical test scenarios**:
- Happy path: a clean checkout runs the gate and the lint, type, and test steps all execute and exit zero, which verifies **AC-1**, **AC-2**, **AC-3** and **AC-9**.
- Failure case: a broker gateway timeout after transmission leaves the order in `UNKNOWN` and a retry is refused until reconciliation, which verifies **AC-7**.
- Failure case: a `SIGTERM` during a live run closes the guard and the event store in order after setting the kill switch, which verifies **AC-5**.
- Auth and permission: an unauthenticated `POST` to the order route is refused with a clear status and never reaches the engine, which verifies **AC-6**.
- Gate integrity: a deliberately introduced failing assertion blocks a pull request, which verifies **AC-8**.

## Build plan

The money path work, the typing ratchet, and the lint wall are all done, and the gate is now green and fully strict. What remains is the two gate verifications (a real end-to-end gate run, and proof it blocks a merge) and the follow-up shim collapse. The already landed items are recorded here as delivered, with their acceptance criteria retained as regression guards rather than build tasks.

**Delivered and guarded (no further build task).** The `runtime_audit` suite now calls the real API and the full suite passes at 4329 (**AC-4**, **AC-3**). Regression guards were added and were verified to fail when the fix is removed: broker timeout to unknown (**AC-7**), SIGTERM lifespan teardown (**AC-5**), authenticated account reads (**AC-6**), the `uv run` gate invocation and the all 17 package coverage floor (**AC-10**, **AC-11**). The lint wall is cleared at baseline 0 and every gated package is at 0 mypy errors, so the gate is fully strict (**AC-1**, **AC-2**). The guard pass also surfaced two genuine non style lint bugs, both fixed: a `NameError` in the `repair_gaps.py` teardown that would have crashed every successful repair run after the work was already written, and a missing import in a parity test. The shim migration additionally fixed a vacuous test (a monkeypatch that targeted the shim rather than the module the code actually used, so it had been a no-op).

1. Confirm on a real gate run that the ratchet, the fourteen type steps, and the test step all execute and pass together. Every step has been verified locally, but not yet end to end on a CI runner, which is the first time the gate has been able to reach the money path tests at all. This satisfies **AC-3** and **AC-9**.
2. Verify the gate blocks a merge by opening a pull request with one deliberately failing assertion and recording that the run fails, then remove the assertion. This satisfies **AC-8**.
3. Collapse the last of the shim tree by moving `TradingSession` into `tradex_runtime`, which retires the cycle-guard shim and the top-level lazy surface. This is a separate architectural change and is tracked as a follow up, not done here.

**Delivered and guarded (no further build task).** The `runtime_audit` suite now calls the real API and the full suite passes at 4329 (**AC-4**, **AC-3**). Regression guards were added and were verified to fail when the fix is removed: broker timeout to unknown (**AC-7**), SIGTERM lifespan teardown (**AC-5**), authenticated account reads (**AC-6**), the `uv run` gate invocation and the all 17 package coverage floor (**AC-10**, **AC-11**). The guard pass also surfaced two genuine non style lint bugs, both fixed: a `NameError` in the `repair_gaps.py` teardown that would have crashed every successful repair run after the work was already written, and a missing import in a parity test.

1. The shim tree is collapsed. 228 test files, 17 scripts and benchmarks, and then 113 leaf shims were migrated or deleted; the tree is down to 27 load-bearing files (import parents, the real sdk code, and the cycle-guard shim). The last batch, moving TradingSession into tradex_runtime so the cycle-guard shim and the top-level lazy surface can go, is a separate architectural change and is deliberately out of this spec. This is the remaining structural work and is tracked as a follow up. The lint wall and typing ratchet are already done and need no further task here.

## Consequences

**Positive**:
- A behavioural regression in the money path can no longer reach the venue unnoticed, because the gate runs tests and a red run blocks a merge.
- The timeout rule that prevents a duplicate live order is guarded by a test that runs in the gate.
- A shut down process leaves no open guard or event store, and a stop cannot silently skip teardown.
- An unauthenticated caller can no longer place or cancel an order.

**Negative / tradeoffs**:
- The lint wall drains over time rather than in one pass, so for a while the gate is green on "no new lint" rather than on "zero lint", and a reader has to check the baseline to know how much style debt remains.
- The typing ratchet reaches full strictness package by package, so the gate is less strict for longer than a single clearing pass would give.
- Now that every account state route requires a credential, any local dashboard or script that read positions, holdings, balance, or order history without one will begin receiving a refusal. This is the intended effect, and the callers must be updated, but it can surface as a confusing failure for a caller that mistakes the refusal for a market or broker fault.
- The pre-commit hook adds a couple of seconds to every commit, which is a real cost paid for a guard that is free to bypass only if nobody bothers.
- The ratchet needs its baseline lowered on purpose after each cleanup, so there is a small amount of deliberate housekeeping that, if forgotten, leaves the baseline stale (which fails safe, since a stale higher baseline is the only way it can drift wrong).

**Neutral**:
- No change to the order lifecycle, the venue contract, the persistence schema, or the datalake.
- No change to the package layout. The shim tree, the `runtime` to `trading` cycle, and the missing ports are untouched here.
- The shipped items from the earlier revision, the non dropping control queue, the replay and order boundary, the multi exchange depth venues, and the fail closed startup cash gate, are unchanged and stay as recorded.
- The two fixes that landed after this spec was first written, ordered teardown and authenticated account reads, are recorded here as delivered rather than planned, and are not reworked.

## Migration plan

**Strategy**: strangler, applied to the gate rather than to the running system. The repair lands incrementally, one gate step at a time, and each step is independently revertible. Nothing about the live trading path changes shape, so no traffic or data is moved.

**Phases**:
1. Phase 1: lint and type. Repair the lint wall and the two hard failing type steps. The gate still does not run tests at the end of this phase, so this phase alone does not make the gate trustworthy. Revert by dropping the lint commits, which changes nothing about runtime behaviour.
2. Phase 2: tests. Repair the audit suite and bring the full run to green. The gate becomes trustworthy at the end of this phase. Revert by dropping the test commits, which leaves the gate unable to reach the test step again, so this phase should not be reverted once phase 3 starts.
3. Phase 3: gate integrity. Add the regression guards for the unknown submission rule and for the two delivered fixes, and confirm a red run blocks a merge. Revert independently of phases 1 and 2, since these are separate tests and no runtime behaviour changes.

**Rollback**: each phase is a separate commit range and reverts on its own. Phase 2 is the one that must not be rolled back once phase 3 begins, because phase 3 assumes the gate can fail. No phase requires a data migration, a code freeze, or coordination with a running process, so rollback is a plain revert at any point.

**Risks**:
- The largest single risk is shipping phase 1 and stopping, which would leave a gate that halts at lint and still never runs the money path tests. The exit condition of phase 2 is the one that matters, and the build should not be called done until the gate run log shows the test step executing.
- Fixing the seven failing tests could be done by loosening the assertions rather than by matching the real API. Each repaired test must be checked so that it asserts the real behaviour of the current tree, and the unknown submission test in particular must fail if the behaviour regresses.
- The two money path fixes that landed recently, completing the idempotency key only after money has moved and letting the order store accept writes from the feed thread, change the failure behaviour of the fill path. Their own regression tests should be reviewed in the same pass that repairs the audit suite, so the new ordering is covered before the gate is trusted to catch a regression.

## Follow-up

- [ ] Write the sibling spec for the missing reactive wire, so that a typed `Candle` is published on the bus and bar driven strategies work in live and paper. This is the largest remaining behavioural gap and it is deliberately not in this spec.
- [ ] Write the sibling spec for the package and ports work: introduce the three ports in `domain`, make the dependency rules mechanically enforced, and collapse the package tree toward twelve real packages. Deferred until the gate is real, because restructuring a codebase whose gate cannot fail is not worth the risk.
- [ ] Remove the continue on error setting from each `mypy` step as its typing debt is cleared, so the gate gets stricter over time rather than weaker.
- [ ] Decide the broker depth protocol: Upstox serves market depth on its market data stream while Dhan serves it on a separate backend, so the shared `MarketStreamPort` cannot declare both without breaking one provider's conformance. This is the last two mypy errors in `brokers` and needs an architecture decision, either a provider specific depth port or a split of the market stream port.
- [ ] Decide whether the SIGTERM path should also close the idempotency guard, the durable event store, and the kill switch. The app lifespan and the `tradex serve` command release the writer lock, broker, bus, and feeds, but those three live in `ExecutionEngine.shutdown()` and are not reached on a signal today. The regression tests document the real behaviour rather than assuming it.
- [ ] Regenerate the project guide and the readme for the current package layout and the real `Quantity` and `Price` construction, which both currently show a call signature that raises a `TypeError`. This is owned by the sync workflow, not by this spec.
- [ ] Decide production browser authentication in a separate decision. The delivered fix uses a session cookie or the legacy API key and leaves the server open when no credential is configured, which is correct for paper and local work but is not a production identity model. The deferred design already exists and this spec only requires authenticated account reads and loopback binding.

## References

**Project sources** (verifiable, in this repo):
- `CLAUDE.md`, the standard interfaces, the fail closed live trading rules, and the guidance on gates that must be able to fail
- `trading-predeploy-plan.md`, the hard gate chain and the rule that no phase starts while the prior gate is red
- `trading-deep-review-findings.md`, the money path sequencing findings and the recorded resolution of the position lock
- `docs/ARCHITECTURAL-AUDIT-2026-09-25.md`, the recorded ratchet approach to typing debt and to a coverage floor that never decreases
- `.github/workflows/quality-gate.yml`, the repaired step order, the `uv run` invocation, the per package typing counts, the lint step left hard, and the coverage floor
- `tests/runtime_audit/`, the suite specified by section E of the earlier revision of this spec
- `a794fbe`, the ASGI lifespan teardown that makes a `SIGTERM` reach the shutdown path
- `e1c23e6`, the authentication added to the 25 previously unprotected HTTP operations
- `docs/target-architecture-design.md`, the fail closed principles this work serves
- spec `0001`, deepen parquet market history, completed and unaffected

**Practices & standards**:
- strangler pattern for repairing a live system incrementally, one step at a time
- idempotency keys for money operations, so a repeated submission is recognised rather than duplicated
- fail closed design, so an unknown outcome is never recorded as a known negative
- continuous integration gates that are able to fail, since a gate that cannot fail is not coverage
