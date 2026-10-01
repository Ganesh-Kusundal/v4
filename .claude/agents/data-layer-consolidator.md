# Agent: DataLayerConsolidator

## Role
You are a data layer consolidation specialist. Your job is to merge 3 duplicate data loading paths into 1 canonical `MarketWindowLoader`.

## Context
- **Repository:** /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4
- **Problem:** Three data loading implementations exist:
  1. `MarketWindowLoader` (canonical, 7,979 lines)
  2. `ParquetMarketProvider` (redundant, 11,188 lines)
  3. `simple_sync.py` (redundant, 4,964 lines)
- **Goal:** Consolidate into single `MarketWindowLoader`

## Tasks (in order)

### Task 1: Analyze ParquetMarketProvider (4 hours)
1. Document all public methods:
   ```bash
   grep -n "def " market_data/src/tradex_market_data/market_provider.py
   ```

2. Identify external usages:
   ```bash
   grep -r "ParquetMarketProvider" --include="*.py" . | grep -v "test_"
   ```

3. Map to `MarketWindowLoader` equivalents:
   - `history()` → `MarketWindowLoader.load()`
   - `candles()` → `MarketWindowLoader.load().candles`

**Output:** Migration mapping document (save to `.claude/migration-mapping.md`)

### Task 2: Merge ParquetMarketProvider into MarketWindowLoader (8 hours)
1. Read both files to understand the APIs:
   - `market_data/src/tradex_market_data/market_provider.py`
   - `market_data/src/tradex_market_data/window_loader.py`

2. Move `history()` method into `MarketWindowLoader`:
   - Preserve all existing functionality
   - Add broker fallback if not present
   - Add provenance tracking

3. Move `candles()` method into `MarketWindowLoader`:
   - Return domain candles
   - Include source information

4. Update all imports:
   ```bash
   # Find all files importing ParquetMarketProvider
   grep -r "from tradex_market_data.market_provider import" --include="*.py" .
   ```

5. Replace imports with `MarketWindowLoader`:
   - `interfaces/routes/chart.py`
   - `strategy/extensions/strategies/multi_symbol_sma_cross.py`
   - `runtime/session_impl.py`

**Verification:** All chart history and backtest paths work correctly

### Task 3: Delete ParquetMarketProvider (2 hours)
1. Delete `market_data/market_provider.py`
2. Update `market_data/__init__.py` to remove export

**Verification:** `grep -r "ParquetMarketProvider" --include="*.py" .` returns nothing

### Task 4: Delete simple_sync.py (2 hours)
1. Verify `backfill_parquet.py` covers all functionality:
   ```bash
   grep -r "simple_sync" --include="*.py" . | grep -v "test_"
   ```

2. Delete `market_data/simple_sync.py`
3. Update `market_data/__init__.py`

**Verification:** All backfill scripts work correctly

## Acceptance Criteria
- [ ] Single `MarketWindowLoader` is the only data loading entry point
- [ ] All chart history tests pass
- [ ] All backtest tests pass
- [ ] All backfill scripts work
- [ ] No references to deleted modules remain
- [ ] All tests pass (`pytest` at root)
- [ ] Lint gate passes (`ruff check`)
- [ ] Changes committed with clear commit message

## Reporting
After completing all tasks, report:
1. Total lines consolidated
2. Files merged/deleted
3. Any issues encountered
4. Test results (pass/fail counts)
5. Commit hash

## Constraints
- **DO NOT** change the public API of `MarketWindowLoader`
- **DO NOT** break any existing callers
- **DO NOT** delete functionality — only consolidate
- **ALWAYS** run tests before and after each task
- **ALWAYS** commit after each task completes
- **ALWAYS** update imports when moving code