# Agent: DeadCodeHunter

## Role
You are a dead code elimination specialist. Your job is to remove provably dead code from TradeX v4 with zero behavioral change.

## Context
- **Repository:** /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4
- **Plan:** `docs/superpowers/specs/2026-10-22-v4-predeploy-remediation-design.md`
- **Goal:** Delete dead code, verify no regressions, commit changes

## Tasks (in order)

### Task 1: Delete Dead Directories (2 hours)
```bash
# Verify no imports exist
grep -r "from poc\|import poc" --include="*.py" .
grep -r "from apps\|import apps" --include="*.py" .

# Delete directories
rm -rf poc/
rm -rf apps/
rm -rf graphify-out/
```

**Verification:** `grep -r "poc\|apps\|graphify" --include="*.py" .` returns nothing

### Task 2: Delete One-Time Migration Scripts (1 hour)
```bash
# Verify scripts are not referenced
grep -r "backfill_2025\|backtest_datalake\|sync_today\|topup_gaps" --include="*.py" .

# Delete scripts
rm trading/scripts/backfill_2025.py
rm trading/scripts/backtest_datalake.py
rm trading/scripts/sync_today.py
rm trading/scripts/topup_gaps.py
```

**Verification:** `grep -r "backfill_2025\|backtest_datalake\|sync_today\|topup_gaps" --include="*.py" .` returns nothing

### Task 3: Delete Deprecated Token Manager (4 hours)
1. Find all usages of `DurableTokenManager`:
   ```bash
   grep -r "DurableTokenManager" --include="*.py" .
   ```

2. Migrate callers to `PortTokenManager`:
   - Replace `DurableTokenManager` with `PortTokenManager`
   - Update constructor arguments as needed

3. Delete `DurableTokenManager` class from `brokers/common/token_lifecycle.py`

4. Delete legacy v2 token file reading code

**Verification:** `grep -r "DurableTokenManager" --include="*.py" .` returns nothing

### Task 4: Delete Legacy Compatibility Layers (2 hours)
1. Delete legacy rate limit re-exports from `brokers/common/resilience.py`
2. Delete legacy v2 field handling code

**Verification:** All tests pass, no legacy imports remain

### Task 5: Delete ParquetBacktestLoader (6 hours)
1. Find all usages:
   ```bash
   grep -r "ParquetBacktestLoader" --include="*.py" .
   ```

2. Migrate callers to `MarketWindowLoader`:
   - `trading/scripts/backtest_datalake.py` (already deleted in Task 2)
   - `strategy/extensions/strategies/multi_symbol_sma_cross.py`

3. Delete `market_data/backtest_loader.py`

4. Update `market_data/__init__.py`

**Verification:** `grep -r "ParquetBacktestLoader" --include="*.py" .` returns nothing

## Acceptance Criteria
- [ ] 25,000+ lines deleted
- [ ] All tests pass (`pytest` at root)
- [ ] No dead imports remain (`grep` verification)
- [ ] Lint gate passes (`ruff check`)
- [ ] Changes committed with clear commit message

## Reporting
After completing all tasks, report:
1. Total lines deleted
2. Files deleted
3. Any issues encountered
4. Test results (pass/fail counts)
5. Commit hash

## Constraints
- **DO NOT** delete code that is still imported anywhere
- **DO NOT** change behavior — only delete dead code
- **DO NOT** refactor live code — that's for Phase 2
- **ALWAYS** run tests before and after each task
- **ALWAYS** commit after each task completes