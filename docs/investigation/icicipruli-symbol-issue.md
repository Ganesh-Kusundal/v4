# ICICIPRULI Symbol Issue - Investigation Report

## Summary

**ICICIPRULI failed to sync during the Oct 23 - Nov 9, 2026 backfill due to a symbol mismatch between the universe CSV and the broker's master data.**

## Root Cause

### The Problem
1. **Universe CSV** lists the symbol as: `ICICIPRULI` (ISIN: INE726G01019)
2. **Dhan Broker** has the symbol as: `ICICILIFE`
3. The registry lookup for `NSE:ICICIPRULI` returns **no provider key**, causing the sync to fail

### Evidence
```python
# Universe CSV
ICICIPRULI - ISIN: INE726G01019

# Broker Master
ICICILIFE - Provider Key: NSE:18652
(No ICICIPRULI found in broker at all)
```

### Historical Context
The datalake **does have historical data** for ICICIPRULI:
- 2022-2025: Full year data (December only for some reason)
- 2026: Jan-Oct 22 data exists

This suggests that either:
1. The symbol was previously accessible but has been renamed by Dhan
2. There was a different mapping mechanism in the past
3. The old data may have come from a different source

## Impact

- **1 out of 500 symbols** failed to sync (0.2% failure rate)
- **Missing data**: Oct 23 - Nov 6, 2026 for ICICIPRULI
- All other 499 symbols synced successfully

## Possible Solutions

### Option 1: Update Universe CSV (Recommended)
Replace `ICICIPRULI` with `ICICILIFE` in the nifty500 universe CSV file if they represent the same company (ICICI Prudential Life Insurance).

**Verification needed**: Confirm via ISIN match that ICICIPRULI = ICICILIFE

### Option 2: Add Symbol Mapping
Create a symbol alias/mapping layer that translates:
- `ICICIPRULI` (universe) → `ICICILIFE` (broker)

This would be handled in the instrument resolution logic.

### Option 3: Manual Data Fetch
Manually fetch the missing ICICIPRULI data using the ICICILIFE symbol and store it under the ICICIPRULI directory.

## Recommended Action

1. **Verify ISIN match** between ICICIPRULI (INE726G01019) and ICICILIFE
2. If confirmed, update the nifty500.csv universe file to use `ICICILIFE` instead of `ICICIPRULI`
3. Run a targeted sync for just this symbol to fill the gap
4. Consider renaming existing ICICIPRULI data directory to ICICILIFE for consistency

## Files to Check

- Universe CSV: Look for nifty500.csv in the project
- Symbol mapping configuration (if any exists)
- Historical data: `data/ohlcv/symbol=ICICIPRULI/`

## Next Steps

Run the universe rename detection to see if this is already being tracked:
```bash
python trading/scripts/sync_universe.py --dry-run
```

This should detect ICICIPRULI → ICICILIFE as a rename candidate if the ISINs match.
