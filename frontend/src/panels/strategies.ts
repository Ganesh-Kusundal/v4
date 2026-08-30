// Strategies panel — discovery-driven UI for backtests and scanners.
// Every list, param, and condition name comes from /api/charts responses;
// this panel hardcodes no formula knowledge. The backend owns all engines:
// BacktestEngine for runs, ScannerEngine for screens.
import { expectJson } from "../http";
export interface StrategyCatalogue {
  strategies: { id: string; strategy_id: string; version: string }[];
  scanners: {
    id: string;
    universe_size: number;
    conditions: string[];
    rank_by: string;
    limit: number;
  }[];
  backtestable: { id: string; params: string[] }[];
}

interface BacktestMetrics {
  total_return: number;
  sharpe: number;
  max_drawdown: number;
  num_trades: number;
  num_rejected: number;
  total_fees: number;
}

interface BacktestResponse {
  metrics: BacktestMetrics | null;
  equity_curve: { time: number; value: number }[];
  trades: {
    time: number;
    side: string;
    price: number;
    qty?: number | null;
    reason: string;
    rejected: boolean;
  }[];
  message?: string;
}

interface ScannerResponse {
  scanner: string;
  results: {
    symbol: string;
    exchange: string;
    score: number;
    matched: string[];
    values: Record<string, number>;
    rank: number;
  }[];
}

/** Callbacks the host supplies so results can land on the chart. */
export interface PanelHost {
  showEquityCurve(points: { time: number; value: number }[]): void;
  showTradeMarkers(trades: BacktestResponse["trades"]): void;
}

export async function fetchStrategyCatalogue(): Promise<StrategyCatalogue> {
  const resp = await fetch("/api/charts/strategies");
  return expectJson<StrategyCatalogue>(resp);
}

async function runBacktest(body: Record<string, unknown>): Promise<BacktestResponse> {
  const resp = await fetch("/api/charts/backtest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return expectJson<BacktestResponse>(resp);
}

async function runScanner(id: string): Promise<ScannerResponse> {
  const resp = await fetch("/api/charts/scanner/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id }),
  });
  return expectJson<ScannerResponse>(resp);
}

/**
 * Build the strategies panel inside `container`. Forms are generated from
 * the catalogue's param lists; a new strategy or scanner appears here with
 * zero JS changes as long as the backend registers it.
 */
export function createStrategiesPanel(
  container: HTMLElement,
  host: PanelHost,
  catalogue: StrategyCatalogue,
  current: { exchange: string; symbol: string },
): void {
  container.innerHTML = "";
  container.className = "panel";

  const title = document.createElement("h3");
  title.textContent = "Strategies";
  container.append(title);

  // --- backtest form -------------------------------------------------------
  const btSection = document.createElement("section");
  const btLabel = document.createElement("label");
  btLabel.textContent = "Backtest";
  const btSelect = document.createElement("select");
  for (const s of catalogue.backtestable) {
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = s.id.replace(/_/g, " ");
    btSelect.append(opt);
  }
  const paramRow = document.createElement("div");
  paramRow.className = "param-row";
  const runBtn = document.createElement("button");
  runBtn.textContent = "Run";
  const status = document.createElement("span");
  status.className = "status";

  function renderParamInputs(): void {
    paramRow.innerHTML = "";
    const entry = catalogue.backtestable.find((s) => s.id === btSelect.value);
    for (const p of entry?.params ?? []) {
      const input = document.createElement("input");
      input.type = "number";
      input.placeholder = p;
      input.dataset.param = p;
      paramRow.append(input);
    }
  }
  btSelect.addEventListener("change", renderParamInputs);
  renderParamInputs();

  runBtn.addEventListener("click", () => {
    void (async () => {
      status.textContent = "running...";
      try {
        const params: Record<string, number> = {};
        for (const input of Array.from(paramRow.querySelectorAll<HTMLInputElement>("input"))) {
          if (input.value !== "") params[input.dataset.param!] = Number(input.value);
        }
        // ~3 months back: wide enough for slow defaults (SMA 20) to fire at
        // least once; the default lookback of /history is far too short.
        const to = Math.floor(Date.now() / 1000);
        const from = to - 90 * 24 * 3600;
        const result = await runBacktest({
          exchange: current.exchange,
          symbol: current.symbol,
          interval: "D",
          strategy: btSelect.value,
          from,
          to,
          ...(Object.keys(params).length ? { params } : {}),
        });
        if (!result.metrics) {
          status.textContent = result.message ?? "no data";
          return;
        }
        status.textContent =
          `${(result.metrics.total_return * 100).toFixed(2)}% | ` +
          `sharpe ${result.metrics.sharpe.toFixed(2)} | ` +
          `${result.metrics.num_trades} trades` +
          (result.metrics.num_rejected ? `, ${result.metrics.num_rejected} rejected` : "");
        host.showEquityCurve(result.equity_curve);
        host.showTradeMarkers(result.trades);
      } catch (err) {
        status.textContent = err instanceof Error ? err.message : String(err);
      }
    })();
  });

  btSection.append(btLabel, btSelect, paramRow, runBtn, status);
  container.append(btSection);

  // --- scanner section -----------------------------------------------------
  const scSection = document.createElement("section");
  const scTitle = document.createElement("label");
  scTitle.textContent = "Scanners";
  const table = document.createElement("table");
  table.className = "scanner-table";

  scSection.append(scTitle, table);
  container.append(scSection);

  for (const scanner of catalogue.scanners) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    const btn = document.createElement("button");
    btn.textContent = `${scanner.id.replace(/_/g, " ")} (${scanner.universe_size})`;
    btn.addEventListener("click", () => {
      void (async () => {
        cell.textContent = "scanning...";
        try {
          const out = await runScanner(scanner.id);
          const lines = out.results
            .slice(0, scanner.limit)
            .map((r) => `${r.rank}. ${r.symbol} (${(r.score * 100).toFixed(0)}%)`);
          cell.innerHTML = "";
          const pre = document.createElement("pre");
          pre.className = "scanner-results";
          pre.textContent = lines.length ? lines.join("\n") : "no matches";
          cell.append(pre);
        } catch (err) {
          cell.textContent = err instanceof Error ? err.message : String(err);
        }
      })();
    });
    cell.append(btn);
    row.append(cell);
    table.append(row);
  }
}
