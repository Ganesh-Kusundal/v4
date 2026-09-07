# tradex-duckdb — Query-Based Analytics Service

Read-only DuckDB analytics over the TradeX OHLCV parquet datalake
(`data/ohlcv/symbol=…/year=…/month=…/data.parquet`), exposed as an MCP
server. Isolated by design: this package imports only `duckdb` + `pyarrow`
(+ `mcp` for the server) and never imports `tradex_trading`,
`tradex_brokers`, or `tradex_domain`.

## Install

```bash
pip install -e "services/duckdb-analytics[mcp]"
```

(Repo venv already has the deps: duckdb 1.x, mcp 1.x, pyarrow.)

## Views

| View | Contents |
|---|---|
| `ohlcv` | Default surface — bars inside the 09:15–15:30 IST session only |
| `ohlcv_raw` | Everything stored, incl. phantom post-market bars (audit) |

Hive partition columns (`symbol`, `year`, `month`) are extracted automatically.
The stored `timestamp` is tz-naive IST wall time; compare with naive
`TIMESTAMP`/`TIME` literals.

## Run the MCP server

```bash
PYTHONPATH=services/duckdb-analytics/src python -m duck_analytics.mcp.server
# path resolution walks up from CWD to find data/ohlcv
```

Register in an MCP client (e.g. Claude Desktop):

```json
{
  "mcpServers": {
    "duck-analytics": {
      "command": ".venv/bin/python",
      "args": ["-m", "duck_analytics.mcp.server"],
      "env": {"PYTHONPATH": "services/duckdb-analytics/src"}
    }
  }
}
```

Or explore interactively: `npx @modelcontextprotocol/inspector`.

### Tools

| Tool | Purpose |
|---|---|
| `query(sql, limit?)` | Raw read-only SELECT/WITH against the views |
| `schema(view?)` | Column list for `ohlcv` / `ohlcv_raw` |
| `list_symbols()` / `date_range(symbol)` | Lake metadata |
| `resample(tf, start, end, symbols_json?)` | Canonical OHLCV aggregation (1m→5m/15m/30m/1h/1d) |
| `run_scan_technical(as_of, …)` | SMA cross-up / RSI-below screen |
| `run_scan_volume(as_of, …)` | Volume > k × prior-N mean screen |
| `run_scan_gap_vol(as_of, …)` | Overnight gap % / intraday range % screen |
| `run_breadth(as_of, dma?)` | % above N-DMA + advances/declines per day |

All scanner tools **require `as_of`** and never see bars after it
(point-in-time safe). The raw `query` tool returns a
`point_in_time_safe=false` marker when unbounded.

## Direct Python use

```python
from pathlib import Path
from duck_analytics import AnalyticsConfig, DuckDBCatalog
from duck_analytics.query import QueryService

cfg = AnalyticsConfig(base_path=Path("data/ohlcv"))
cat = DuckDBCatalog(cfg)
svc = QueryService(cat, cfg)

res = svc.execute(
    "SELECT symbol, last(close ORDER BY ts) AS c FROM ohlcv "
    "WHERE ts::DATE = DATE '2026-08-21' GROUP BY symbol LIMIT 10"
)
print(res.columns, res.rows)
cat.close()
```

## Guarantees & guards

- SELECT/WITH only — INSERT/DDL/ATTACH/COPY/INSTALL/PRAGMA/CALL rejected;
  multi-statement strings rejected.
- Server-side row cap (default 1000, max 50 000) + `truncated` flag +
  best-effort `total_row_count`; caller-provided SQL limits cannot bypass the
  service cap, and strict callers fail on truncation.
- Memory/thread caps (`4GB`, `4`) on the DuckDB connection.
- Retry-once on torn reads while `ParquetStorage.upsert` rewrites a partition.
- Results include a dataset fingerprint derived from configured input-file
  metadata and the session policy. This identifies the observed input set; it
  is not an immutable content snapshot, so reproducible studies should pin an
  immutable lake copy or manifest externally.
- Indicator parity: SMA identical to `analytics/indicators.sma`; RSI is exact
  Wilder smoothing via closed-form decay sum, golden-tested against
  `analytics/indicators.rsi`. Guard: keep scanned bars per symbol ≲9000.

## Relationship to the rest of TradeX

Fast-analysis path only. Backtests stay on the real infra:
screener output → `ParquetBacktestLoader(instruments=symbols)` →
`BacktestEngine.run(strategy)` (same ExecutionEngine spine as paper/live).
`ScannerEngine` remains the live/in-session scanner; this service is the
offline research layer. Nothing else in the repo changes.

## Tests

```bash
cd services/duckdb-analytics && ../../.venv/bin/python -m pytest tests -q
```
