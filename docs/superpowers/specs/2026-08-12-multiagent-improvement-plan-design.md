# Multi-Agent Improvement Plan for TradeX v4 — Design

Date: 2026-08-12
Status: Approved

## Context

TradeX v4 is a broker-agnostic algorithmic trading platform for Indian markets
with a strict three-package dependency boundary (`domain ← brokers ← trading`).
A prior architecture review identified 16 improvement opportunities across five
priority areas. This design decomposes the full improvement roadmap into
disjoint units of work belonging to a 5-agent team, coordinated via git
worktrees and branches with a strict verification gate.

## Goals

- Fix all 38 pre-existing test failures (v3→v4 migration gaps)
- Polish execution architecture (fill parity, wire tags, rate-limit, correlation)
- Harden quality gates (mypy in CI, Ruff hooks, parity contracts)
- Improve multi-broker robustness (Dhan 5-day guard, phantom-bar stripping)
- Document decisions (ADRs) and lower the live-mode onboarding barrier

## Non-Goals

- No new broker integrations beyond the existing Dhan/Upstox/Paper adapters
- No changes to the public `BrokerAdapter`/`ExtensionAdapter` protocol surface
- No rewrite of the reactive bus or execution engine spine — only targeted fixes

## Team Structure (5 Agents, Domain-Sliced)

| Agent | Domain Ownership | Work Items |
|-------|-----------------|------------|
| **Agent 1: Test-Fix** | `trading/src/tradex_trading/execution/`, `replay/`, `sdk/session.py` | Fix 38 pre-existing test failures: backtest PnL (`test_backtest_real_pnl.py`), live fill bridge stamping (`test_live_fill_stream_bridge.py`), integration tests (`test_full_stack.py`), session factory tests |
| **Agent 2: Architecture-Polish** | `execution/fill_sources.py`, `engine.py`, `domain/wire.py` | Unified `FillModel` base class (fill parity across backtest/replay/paper/live), `max_orders_per_minute` enforcement in the reactive pipeline, `InstrumentId.asset_class` → wire tag derivation, correlation_id round-trip validation |
| **Agent 3: Quality-Gates** | `.github/workflows/`, `pyproject.toml`, `trading/tests/parity`, `trading/tests/contracts`, pre-commit config | Add `mypy` step to CI, expand parity contract tests (golden event stream → identical fills/positions/P&L across modes), Ruff pre-commit hooks, reclassify/fix remaining lint issues |
| **Agent 4: Broker-Robustness** | `datalake/parallel_fetcher.py`, `datalake/parquet_storage.py`, `brokers/src/tradex_brokers/dhan/`, `upstox/` | Dhan 5-day API history guard (fail loud instead of silent truncation), phantom post-market bar stripping on `ParquetStorage.read()`, Upstox multi-month history routing |
| **Agent 5: Docs-Onboarding** | `docs/`, `CLAUDE.md`, `trading/scripts/` | New ADRs (fill parity, wire tags, Dhan guard), `quick_start_live.py` bootstrap script, graphify setup integration |

## Coordination Via Git Worktrees

- Each agent works in its own git worktree branch off `main`:
  `improve/agent1-test-fix`, `improve/agent2-arch`, `improve/agent3-quality`,
  `improve/agent4-brokers`, `improve/agent5-docs`
- **Merge order (dependency-driven):**
  1. Agent 1 (Test-Fix) — foundation; establishes green baseline
  2. Agent 2 (Architecture-Polish) — depends on stable tests to surface regressions
  3. Agent 3 (Quality-Gates) + Agent 4 (Broker-Robustness) — disjoint, parallel
  4. Agent 5 (Docs-Onboarding) — documents decisions made by 1-4
- **Conflict rule:** if two agents touch the same file, the later agent must
  `git rebase` onto the merged branch before continuing; rebase is done by the
  orchestrator, not the agent

## Verification Gate (Strict)

Every agent must demonstrate ALL of the following before its branch is eligible
for merge:

1. `python -m pytest domain/tests brokers/tests trading/tests` — all green
2. `ruff check domain/src brokers/src trading/src` — 0 errors
3. `mypy domain/src/tradex_domain` — clean; new code must not add errors to
   brokers/trading (pre-existing strict-mode errors are documented, not fixed
   by this plan)
4. `graphify update .` — knowledge graph current
5. `git diff --stat` — only the agent's owned files changed

## Delivery Contract

- Each agent commits work to its branch with a descriptive, conventional commit
  message matching the repo style
- Merge only after the strict gate passes on that branch (orchestrator verifies)
- Final orchestration: merge all branches in dependency order → run full test
  suite once more → update graph → confirm zero diff on merged `main`
- New CI workflow `quality-gate.yml` runs on every PR:
  `pytest` → `ruff check` → `mypy domain`

## Key Design Decisions

- **Fill model parity is the highest-risk item** (touches the engine spine for
  all modes) → assigned to Agent 2, sequenced AFTER Agent 1 stabilizes tests so
  regressions surface against known-good baselines
- **5 agents, not 6** — each owns a genuinely disjoint area; the orchestrator
  (not an agent) handles merge coordination
- **Docs committed last** — ADRs must document decisions 1-4 actually made;
  writing them first risks describing unimplemented behavior
- **Strict gate, opt-in pre-existing mypy errors** — the domain package is clean
  today; brokers/trading errors are accepted but must not increase

## Risks / Open Questions

| Risk | Mitigation |
|------|------------|
| Agent 1 & 2 both touch `engine.py` | Dependency-ordered merge; rebase before later merge |
| Fill parity changes backtest P&L numbers | Agent 1's green baseline is the contract — any drift is a regression test failure |
| Dhan 5-day guard could break existing backfills | Guard is fail-loud (clear error) not silent-skip; backfill scripts updated in Agent 4 |
| Quality-gate CI changes may slow every PR | Mypy restricted to domain (already clean, ~1s); full mypy remains manual |
| Worktree merge conflicts accumulate | Orchestrator performs rebases; agents never merge each other |

## Success Criteria

- All targeted test failures fixed; full suite green on merged `main`
- `ruff` clean; `mypy domain` clean in CI
- Fill model parity verified by parity contract tests across backtest/paper/live
- Dhan history requests > API window fail with a clear, actionable error
- New ADRs exist that a new engineer can read to understand fill/wire/Dhan choices
- `graphify update .` reflects the final `main` state