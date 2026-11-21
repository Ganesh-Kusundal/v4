# Vibe-Trading Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the HKUDS/Vibe-Trading reference reproducible and expose a narrow, read-only TradeX MCP contract that an external agent (including Vibe-Trading) can consume without weakening TradeX boundaries or safety gates.

**Architecture:** Keep TradeX as the deterministic source of truth (domain, market data, replay/backtest, runtime, broker snapshots). Treat `vibe-trading/` as an external/reference clone outside the uv workspace. Extend the existing `tradex_interfaces.agent` stdio MCP server with versioned read-only tools and JSON-safe schemas; no order-capable tools. Any Vibe-Trading-side adapter is a separate follow-up spike, not a workspace dependency.

**Tech Stack:** Python 3.12, uv workspace, pytest, FastAPI/uvicorn (existing), TradeX MCP stdio JSON-RPC server (`tradex_interfaces.agent`), git/submodule or bootstrap script for external reference repo.

**Spec:** `docs/superpowers/specs/2026-11-21-vibe-trading-integration-design.md`

## Global Constraints

- Do **not** add `vibe-trading/` to uv workspace members, root `pyproject.toml` dependencies, or import-linter contracts as an internal package.
- Do **not** weaken `tests/test_import_boundaries.py`, `tests/test_packaging_authority.py`, import-linter contracts, or ruff/mypy/coverage ratchets.
- All new agent tools are **read-only** in Phase 1: no place/modify/cancel order, no credential mutation, no live-trading toggle.
- Tool outputs must be JSON-safe dicts/lists/primitives; timestamps ISO-8601; money/price/qty serialized explicitly (string or float only where existing tool contract already uses float - be consistent and document).
- Use existing TradeX standard interfaces: `tradex_config.env.load_env_file`, `tradex_runtime.live.build_broker_from_env`, `MarketWindowLoader`/`ParquetStorage`/`ParallelHistoryFetcher` patterns per CLAUDE.md; do not hand-roll broker auth.
- Keep secrets out of git; `.env.local` remains local/ignored.
- Any live broker snapshot tool must require explicit opt-in config and preserve existing live confirmation/risk gates; default to paper/no-live.

## Review Focus

- **Untrusted/external symbol input**: `market.history`/`market.quote` must reject or normalize bad exchange/symbol/interval values with a clear error, not traceback or arbitrary file/path access.
- **Accidental order capability**: MCP `tools/list`/dispatch must never expose place/modify/cancel/kill-switch actions in Phase 1, including by alias or future refactor.
- **Live credential leakage**: errors/logs/tool responses must not include tokens, TOTP secrets, API keys, or raw auth headers.
- **Datalake/window edge cases**: empty window, missing symbol, timezone-naive/aware timestamps, and Dhan/Upstox range quirks must return explicit errors/metadata, not silent empty success.
- **Reproducibility drift**: `vibe-trading/` clone/bootstrap must be pinned/documented; local `.git/info/exclude`-only state is not enough for another developer.

---

## File Structure

| File | Responsibility |
|---|---|
| `docs/superpowers/specs/2026-11-21-vibe-trading-integration-design.md` | Decision/spec record; already drafted, updated as decisions land. |
| `docs/runbooks/vibe-trading-integration.md` | Operator/dev runbook: bootstrap clone, run TradeX MCP server, example MCP calls, safety boundaries. |
| `scripts/bootstrap_vibe_trading.sh` | Reproducible pinned clone/update of HKUDS/Vibe-Trading into `vibe-trading/` (or verifies submodule) without adding to workspace. |
| `.gitignore` or `.git/info/exclude` policy (decide) | Keep clone ignored if bootstrap-managed; if submodule chosen, document instead of ignore. |
| `interfaces/src/tradex_interfaces/agent/tools.py` | Read-only MCP tool implementations and JSON-safe serialization. |
| `interfaces/src/tradex_interfaces/agent/server.py` | MCP dispatch/registry; advertises tool specs. |
| `interfaces/src/tradex_interfaces/agent/schemas.py` (create) | Optional: input validation/normalization helpers for agent tool args (exchange/symbol/interval/window). Keep small and dependency-free. |
| `interfaces/tests/test_agent_tools.py` | Unit tests for tool behavior/serialization/error cases. |
| `interfaces/tests/test_agent_server.py` | MCP protocol tests: tools/list, tools/call, unknown tool, notifications. |
| `interfaces/tests/test_agent_safety.py` (create) | Guard tests: no order-capable tools exposed; error redaction/safe failures. |
| `README.md` or `docs/README` index (only if needed) | Link to runbook/spec; avoid churn. |

## Task 1: Confirm Decisions and Freeze Phase 1 Scope

**Files:**
- Modify: `docs/superpowers/specs/2026-11-21-vibe-trading-integration-design.md`
- Test: N/A (decision gate)

**Interfaces:**
- Consumes: existing repo state, this plan, user answers.
- Produces: confirmed answers for open questions (MCP-only vs HTTP, clone strategy, first capability, live data scope, vendoring policy).

- [ ] **Step 1: Ask user to review spec and answer open questions**

Present the 5 open questions from the spec and recommend defaults:
1. MCP-only first; HTTP after contract stabilizes.
2. Bootstrap script + pinned commit in docs/script, keep clone ignored (no submodule) unless team wants git-tracked pin.
3. First capability: external agent calls TradeX `backtest.run` + `market.history` + `strategies.list`.
4. Read-only paper/local-datalake first; live broker snapshot only with explicit opt-in.
5. No vendoring in Phase 1.

- [ ] **Step 2: Record decisions in spec**

Update “Open Questions” with decisions and date/owner. Add a short “Phase 1 Accepted Scope” section.

- [ ] **Step 3: Commit decision doc**

```bash
git add docs/superpowers/specs/2026-11-21-vibe-trading-integration-design.md
git commit -m "docs: record vibe-trading integration phase 1 decisions"
```

## Task 2: Make Vibe-Trading Reference Reproducible

**Files:**
- Create: `scripts/bootstrap_vibe_trading.sh`
- Create: `docs/runbooks/vibe-trading-integration.md`
- Modify: `.gitignore` (only if needed for bootstrap-managed clone policy)
- Test: manual shell verification (script smoke)

**Interfaces:**
- Consumes: pinned upstream URL/commit decided in Task 1.
- Produces: deterministic local clone/bootstrap flow; runbook.

- [ ] **Step 1: Write a failing smoke check (shell)**

Run: `test -x scripts/bootstrap_vibe_trading.sh`
Expected: FAIL before script exists.

- [ ] **Step 2: Implement `scripts/bootstrap_vibe_trading.sh`**

Script contract:
- `set -euo pipefail`
- Defaults: `REPO_URL=https://github.com/HKUDS/Vibe-Trading.git`, `TARGET_DIR=vibe-trading`, `PINNED_COMMIT=<decided>`
- If target missing: `git clone --depth 1 ...` then fetch/checkout pinned commit if pin specified.
- If target exists and is git repo: verify remote, fetch pinned commit, checkout pinned commit (no destructive reset unless explicit `--force`).
- Print resolved commit SHA.
- Never modify uv workspace files.

- [ ] **Step 3: Verify script behavior**

Run: `bash scripts/bootstrap_vibe_trading.sh`
Expected: exits 0, prints pinned SHA, `git -C vibe-trading rev-parse HEAD` matches pin.

- [ ] **Step 4: Write runbook**

`docs/runbooks/vibe-trading-integration.md` must include: purpose, safety boundary, bootstrap command, how to start TradeX MCP server, example `tools/list` / `tools/call`, and “no order tools in Phase 1”.

- [ ] **Step 5: Commit**

```bash
git add scripts/bootstrap_vibe_trading.sh docs/runbooks/vibe-trading-integration.md .gitignore
git commit -m "chore: add reproducible vibe-trading reference bootstrap"
```

## Task 3: Harden Agent Tool Input Validation

**Files:**
- Create: `interfaces/src/tradex_interfaces/agent/schemas.py`
- Modify: `interfaces/src/tradex_interfaces/agent/tools.py`
- Test: `interfaces/tests/test_agent_tools.py`

**Interfaces:**
- Consumes: raw MCP `arguments` dicts.
- Produces: validated/normalized args: `exchange: str`, `symbol: str`, `interval: str`, `start/end: datetime`, bounded `initial_capital`, safe `params` dict.

- [ ] **Step 1: Write failing tests for bad inputs**

Add tests in `interfaces/tests/test_agent_tools.py`:
- unknown interval -> `ValueError` with clear message
- empty symbol/exchange -> `ValueError`
- invalid ISO date -> `ValueError`
- `initial_capital <= 0` -> `ValueError`
- `params` non-dict -> `ValueError`

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync python -m pytest interfaces/tests/test_agent_tools.py -q`
Expected: FAIL for new validation cases.

- [ ] **Step 3: Implement validation helpers**

In `schemas.py`, add small pure helpers, e.g.:
```python
def normalize_symbol(exchange: str, symbol: str) -> tuple[str, str]: ...
def parse_iso_datetime(value: str, *, name: str) -> datetime: ...
def normalize_interval(interval: str) -> str: ...
def normalize_backtest_args(args: dict[str, Any]) -> dict[str, Any]: ...
```
Use these from `tools.backtest` before loading candles. Keep no external deps.

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run --no-sync python -m pytest interfaces/tests/test_agent_tools.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add interfaces/src/tradex_interfaces/agent/schemas.py interfaces/src/tradex_interfaces/agent/tools.py interfaces/tests/test_agent_tools.py
git commit -m "feat(agent): validate MCP backtest tool inputs"
```

## Task 4: Add Read-Only Market History Tool

**Files:**
- Modify: `interfaces/src/tradex_interfaces/agent/tools.py`
- Modify: `interfaces/src/tradex_interfaces/agent/server.py`
- Modify: `interfaces/tests/test_agent_tools.py`
- Modify: `interfaces/tests/test_agent_server.py`

**Interfaces:**
- Consumes: `symbol`, `exchange`, `interval`, `start`, `end`, optional `limit`.
- Produces: JSON-safe dict: `{symbol, exchange, interval, start, end, source, count, bars:[...]}`.

- [ ] **Step 1: Write failing tests**

Tool test: monkeypatch candle loader to return known candles; assert `market_history(...)` returns JSON-safe count/bars and source metadata.
Server test: `tools/list` includes `market_history`; `tools/call` dispatches it.

- [ ] **Step 2: Run tests to verify fail**

Run: `uv run --no-sync python -m pytest interfaces/tests/test_agent_tools.py interfaces/tests/test_agent_server.py -q`
Expected: FAIL with unknown tool/function.

- [ ] **Step 3: Implement minimal tool**

Add `TOOL_SPECS` entry for `market_history`. Implement:
```python
def market_history(*, symbol: str, exchange: str = "NSE", interval: str = "1D", start: str, end: str, limit: int | None = None) -> dict[str, Any]: ...
```
Reuse existing candle loading path (`_load_candles` / `MarketWindowLoader`) and `bars_to_records`. Include `source: "datalake"` (or actual source field if available) and explicit empty-window error.

- [ ] **Step 4: Verify pass**

Run: `uv run --no-sync python -m pytest interfaces/tests/test_agent_tools.py interfaces/tests/test_agent_server.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add interfaces/src/tradex_interfaces/agent/tools.py interfaces/src/tradex_interfaces/agent/server.py interfaces/tests/test_agent_tools.py interfaces/tests/test_agent_server.py
git commit -m "feat(agent): add read-only market history MCP tool"
```

## Task 5: Add Safety Guard Tests for Agent Surface

**Files:**
- Create: `interfaces/tests/test_agent_safety.py`
- Modify: `interfaces/src/tradex_interfaces/agent/server.py` (only if guard needed)
- Test: `interfaces/tests/test_agent_safety.py`

**Interfaces:**
- Consumes: MCP tool specs and dispatch.
- Produces: regression guard that order-capable tool names are absent.

- [ ] **Step 1: Write failing/guard tests**

Tests should assert:
- no tool name contains `order`, `cancel`, `modify`, `place`, `kill`, `enable_live`, `disable_live` (allowlist/denylist per decision; prefer explicit allowlist of read-only names).
- `tools/call` for `place_order` returns `isError`/unknown tool.
- error responses do not echo secrets from fake exception messages containing `token=...` patterns (if redaction helper added).

- [ ] **Step 2: Run test to verify behavior**

Run: `uv run --no-sync python -m pytest interfaces/tests/test_agent_safety.py -q`
Expected: PASS once implemented; if it catches gap, fix before green.

- [ ] **Step 3: Implement minimal guard**

If needed, add explicit allowlist in server dispatch or tool registry assertion. Do not add order tools.

- [ ] **Step 4: Verify pass**

Run: `uv run --no-sync python -m pytest interfaces/tests/test_agent_safety.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add interfaces/tests/test_agent_safety.py interfaces/src/tradex_interfaces/agent/server.py
git commit -m "test(agent): guard MCP surface against order-capable tools"
```

## Task 6: Document External Integration and Smoke It

**Files:**
- Modify: `docs/runbooks/vibe-trading-integration.md`
- Modify: `docs/superpowers/specs/2026-11-21-vibe-trading-integration-design.md` (status/scope updates)
- Test: manual smoke commands

**Interfaces:**
- Consumes: implemented MCP tools.
- Produces: reproducible smoke procedure for an external MCP client.

- [ ] **Step 1: Add smoke procedure to runbook**

Include commands:
```bash
uv run --no-sync tradex-agent  # stdio server
```
And example JSON-RPC messages for `initialize`, `tools/list`, `tools/call backtest`, `tools/call market_history`. Document expected fields and safety notes.

- [ ] **Step 2: Run smoke checks**

Run: `uv run --no-sync python -m pytest interfaces/tests/test_agent_tools.py interfaces/tests/test_agent_server.py interfaces/tests/test_agent_safety.py -q`
Expected: PASS.

Run a stdio smoke with a short Python one-liner piping initialize/tools/list to `serve` or the CLI entry, capturing output. Expected: valid JSON-RPC responses, no order tools.

- [ ] **Step 3: Update spec status**

Mark Phase 1 scope items done/pending with evidence.

- [ ] **Step 4: Commit**

```bash
git add docs/runbooks/vibe-trading-integration.md docs/superpowers/specs/2026-11-21-vibe-trading-integration-design.md
git commit -m "docs: add vibe-trading MCP integration runbook and smoke"
```

## Task 7: Optional Spike — Vibe-Trading as External MCP Client (No Workspace Coupling)

**Files:**
- Create: `docs/superpowers/spikes/2026-11-21-vibe-trading-mcp-client-spike.md`
- Optionally create: `scripts/spike_vibe_trading_mcp_client.sh` (outside package imports)
- Test: manual spike report

**Interfaces:**
- Consumes: running `tradex-agent` stdio MCP server; local `vibe-trading/` clone.
- Produces: evidence report - can Vibe-Trading call TradeX tools, what adapter glue is needed, what not to do.

- [ ] **Step 1: Define spike questions**

Record: Can Vibe-Trading consume an external MCP server endpoint? Which transport fits? What auth/config is needed? What schema mismatches exist?

- [ ] **Step 2: Run minimal spike**

Do not modify `vibe-trading/` tracked upstream behavior; if local experiment needed, keep it disposable and document diffs. Capture commands/output summary.

- [ ] **Step 3: Write spike report**

Include: worked, didn’t work, required adapter shape, security concerns, recommended next phase. No production code merged from spike without review.

- [ ] **Step 4: Commit report**

```bash
git add docs/superpowers/spikes/2026-11-21-vibe-trading-mcp-client-spike.md
git commit -m "docs: record vibe-trading MCP client spike results"
```

## Final Verification

- [ ] **Run focused tests**

```bash
uv run --no-sync python -m pytest interfaces/tests/test_agent_tools.py interfaces/tests/test_agent_server.py interfaces/tests/test_agent_safety.py -q
```
Expected: PASS.

- [ ] **Run boundary/packaging guards**

```bash
uv run --no-sync python -m pytest tests/test_packaging_authority.py tests/test_import_boundaries.py tests/test_removed_surfaces.py -q
```
Expected: PASS.

- [ ] **Run ratchets if code changed broadly**

```bash
uv run --no-sync python scripts/ruff_ratchet.py
uv run --no-sync python scripts/mypy_ratchet.py
```
Expected: no regression beyond baseline policy; fix new errors before merge.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-11-21-vibe-trading-integration.md`. Please review the plan. Which execution approach would you prefer?

- **Subagent-driven** - A fresh subagent implements each task and a fresh reviewer checks it before the next one starts, then a whole-branch review at the end. Most thorough; costs a fresh context per task and per review.
- **Native** - I implement every task myself in this session, the way this harness runs work, then one fresh reviewer on the most capable model checks the whole branch. Cheapest and fastest; no independent review until the end. Runs well with a mid-tier session model, since the plan carries the design.

For this plan I recommend **Native for Tasks 1-3 and 5-6, Subagent-driven if you want Task 4/7 expanded into parallel implementation**, because the tasks are mostly sequential, touch a small surface (`interfaces/src/tradex_interfaces/agent/*`, docs, scripts), and the key risk is safety/boundary regression rather than broad parallel feature work. Does the plan capture what you want, and which approach should we use?
