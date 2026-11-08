# Universe Sync Manager — Operator Runbook

## Overview

The Universe Sync Manager keeps the NSE 500 datalake aligned with current constituents, handles symbol renames, and retries failed syncs.

## Quick Start

### Dry Run (Recommended First)

```bash
python trading/scripts/sync_universe.py --dry-run
```

Shows what would change without modifying anything.

### Full Sync

```bash
python trading/scripts/sync_universe.py \
  --universe nifty500 \
  --start-date 2024-01-01 \
  --end-date 2024-12-31
```

### Skip Phases

```bash
# Skip rename detection
python trading/scripts/sync_universe.py --skip-rename

# Skip retry
python trading/scripts/sync_universe.py --skip-retry
```

## Metadata Files

All metadata lives under `data/`:

- `.universe_metadata.json` — Last refresh timestamp, added/removed symbols
- `.rename_log.json` — History of symbol renames with row counts
- `.sync_failures.json` — Persistent failure queue with retry scheduling

## Troubleshooting

### Rename Conflict

If both old and new symbol directories exist:

```
Rename conflict: both HEG and HEGAM have data — manual merge required
```

**Resolution:** Manually merge parquet files, then delete old directory.

### Permanent Failures

Symbols marked `permanent_failure` after 3 retry attempts:

```bash
# Check failure queue
cat data/.sync_failures.json | jq '.[] | select(.reason == "permanent_failure")'
```

**Resolution:** Investigate manually, then remove from failure queue if resolved.

### Corrupted Metadata

If JSON files become corrupted:

```bash
# Backup and regenerate
cp data/.sync_failures.json data/.sync_failures.json.bak
rm data/.sync_failures.json
# Next sync will start fresh
```

## Monitoring

Check sync health:

```bash
# Last refresh
cat data/.universe_metadata.json | jq '.last_refreshed'

# Pending failures
cat data/.sync_failures.json | jq 'keys | length'

# Recent renames
cat data/.rename_log.json | jq 'keys'
```

## Off-Peak Scheduling

For large retry batches, schedule during off-peak hours (2 AM - 6 AM IST):

```bash
# Schedule via cron
0 2 * * * cd /path/to/repo && python trading/scripts/sync_universe.py --off-peak
```
