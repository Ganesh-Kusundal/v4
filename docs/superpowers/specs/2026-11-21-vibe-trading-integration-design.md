# Vibe-Trading Integration Design (TradeX v4)

## Status

Draft - prepared 2026-11-21 on branch `refactor/phase1-safety`. Needs human review before implementation.

## Context

- TradeX v4 is a broker-agnostic Indian-markets trading platform: frozen domain, Decimal money, RxPY bus, broker adapters, Parquet datalake, deterministic replay/backtest, strict import-linter boundaries.
- HKUDS/Vibe-Trading is cloned locally at `vibe-trading/` (shallow, `origin https://github.com/HKUDS/Vibe-Trading.git`, HEAD `25c89a0`). It is ignored by `.git/info/exclude` and excluded from ruff (`pyproject.toml`). It is a **reference clone**, not integrated.
- TradeX already has a small deterministic MCP/agent surface: `interfaces/src/tradex_interfaces/agent/` (`backtest`, `list_strategies`, stdio JSON-RPC MCP). Commit `50cfd190`.
- No adapter, bridge, packaging, deployment, or contract currently wires Vibe-Trading into TradeX.

## Problem

We want to evaluate and use Vibe-Trading capabilities with TradeX without compromising TradeX's deterministic core, boundary discipline, live-safety gates, or maintainability.

## Goals

1. Make the Vibe-Trading reference reproducible and clearly bounded.
2. Define a narrow, versioned integration contract where TradeX is the source of truth for market data, backtest, execution state, and risk-gated actions.
3. Enable Vibe-Trading (or a Vibe-Trading-inspired agent) to consume TradeX capabilities through MCP/HTTP, read-only first.
4. Keep any live/order-capable path behind explicit TradeX confirmation/risk/config gates.

## Non-Goals

- Merging Vibe-Trading source into TradeX workspace packages.
- Making Vibe-Trading a dependency of `tradex_domain` or any core package.
- Exposing live order placement through an agent/MCP tool before a dedicated safety design is reviewed.
- Replacing TradeX backtest/data/execution with Vibe-Trading implementations.
- Supporting Vibe-Trading's full product surface (channels, shadow accounts, desktop, pixel office, etc.) in phase 1.

## Decision

**Recommended: Option A/B hybrid — TradeX as deterministic infra, narrow contract, Vibe-Trading as external/reference consumer.**

- TradeX owns: instruments, market data/datalake, deterministic backtest/replay, broker adapters, execution/risk, audit/event store.
- Vibe-Trading owns (if used): agent UX, research orchestration, LLM prompting/workflow, optional UI - but only through contracts.
- Integration boundary: MCP (preferred first, aligns with existing TradeX agent server) or local HTTP; read-only by default.
- Vibe-Trading clone remains outside uv workspace members and lint/test gates unless explicitly promoted by a later spec.

## Alternatives Considered

| Option | Summary | Why not now |
|---|---|---|
| A. TradeX infra, selective port | Port useful ideas into TradeX | Good long-term, but slower; use as follow-up after contract proves value. |
| B. Side-by-side services | Run Vibe-Trading separately, call TradeX | Recommended as deployment shape; still needs contract/spec first. |
| C. Embed/fork into repo | Vendor/fork Vibe-Trading into workspace | High dependency/architecture conflict; rejected for phase 1. |

## Proposed Contract (Phase 1, read-only)

Expose through TradeX MCP/agent surface (extend `interfaces/src/tradex_interfaces/agent/`):

| Tool | Purpose | Source of truth |
|---|---|---|
| `strategies.list` / existing `list_strategies` | List registered strategies | `tradex_strategy.extensions` |
| `backtest.run` / existing `backtest` | Deterministic backtest over datalake window | `tradex_replay.backtest.BacktestEngine` |
| `market.history` | OHLCV history window | `tradex_market_data` / broker history interface |
| `market.quote` | Latest quote/LTP | broker/market feed |
| `instruments.resolve` | Resolve symbol/exchange to instrument | `tradex_domain` + instrument registry |
| `account.snapshot` | Positions/funds/orders snapshot, paper or explicitly configured read-only live | runtime/session/broker |

All tools return JSON-safe dicts, include explicit `source`, `as_of`, and error shape. No tool places/modifies/cancels orders in phase 1.

## Safety / Risk

- Default deny for order placement, modification, cancellation, credential mutation, and live-trading toggles.
- Any future write tool requires a separate spec, explicit user confirmation gate, risk-manager checks, and audit logging.
- Vibe-Trading's own trading connectors must not be wired to real credentials through TradeX integration without review.
- Keep secrets in `.env.local` / env; never copy Vibe-Trading config secrets into committed files.

## Reproducibility

Current clone is local-only (`.git/info/exclude`). Phase 1 should add a documented, pinned bootstrap path (script or submodule decision) without making the clone a workspace dependency.

## Open Questions (needs human decision)

1. **Integration mode for first usable milestone:** MCP-only against local TradeX process, or HTTP API, or both?
2. **Should `vibe-trading/` become a pinned git submodule**, or remain a bootstrap-script-managed ignored clone?
3. **Which Vibe-Trading capability is the first target:** research agent UX calling TradeX backtest/history, or shadow/paper workflow, or something else?
4. **Live data scope:** paper-only, or read-only live broker snapshots behind existing live gates?
5. **Do we want to vendor small Vibe-Trading components into TradeX later**, or always keep external?

## Acceptance Criteria (Phase 1)

- A developer can reproduce the Vibe-Trading reference checkout from repo docs/scripts.
- TradeX MCP server exposes documented read-only tools with tests.
- A simple external MCP client can call TradeX tools for history/backtest/list strategies without importing Vibe-Trading code.
- No core TradeX boundary/import-linter guard is weakened.
- No order-capable agent tool is exposed.
