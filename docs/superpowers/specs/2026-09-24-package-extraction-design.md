# Package extraction design (strangler)

**Date:** 2026-09-24  
**Status:** Complete — all target packages extracted  
**Plan:** `docs/deep-refactor-plan.md` Phase 10 + `docs/superpowers/plans/2026-09-24-package-extraction-waves.md`

## Goal

Promote clean `tradex_trading` subtrees into workspace packages without a big-bang import rewrite.

## Workspace shape (done)

```text
tradex-domain
tradex-brokers
tradex-research
tradex-operations
tradex-observability
tradex-analytics
tradex-reactive
tradex-execution
tradex-strategy
tradex-replay
tradex-application
tradex-market-data
tradex-interfaces
tradex-runtime
tradex-persistence
tradex-trading          (composition root + compatibility shims)
```

## Rules

1. New package owns implementation under `{pkg}/src/tradex_*`.
2. `tradex_trading.<old>` remains an importlib re-export shim.
3. Import gates in `tests/test_import_boundaries.py` enforce the graph.
4. Cycle break: `FeedReady` protocol in execution; metrics in observability; lazy `ParquetBacktestLoader` in market_data.
5. No commit unless asked.

## Verify

```bash
uv sync
.venv/bin/python -m pytest \
  tests/test_import_boundaries.py \
  trading/tests/execution trading/tests/strategy trading/tests/application \
  trading/tests/research trading/tests/runtime/test_feed_supervisor.py \
  trading/tests/runtime/test_oms_boot_recovery.py \
  trading/tests/interface/test_fastapi_app.py \
  observability/tests research/tests operations/tests persistence/tests \
  -q
```
