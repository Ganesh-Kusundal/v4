// Seasonality table: tradex_trading computes the year×month heatmap
// (POST /api/charts/profiles/seasonality); this module renders the returned
// {rows, options} through the library's ChartTable primitive. This is the
// reference's one remaining non-curve study (SEASONALITY.table), surfaced as a
// pane primitive because createTier2Indicator has no `table` hook.
//
// createTier2Indicator does not forward a table hook (verified against
// external.ts:142-222 — only calc/levels/range/subscribe), so the table is not
// an indicator; it is a screen-space primitive anchored per options.position.
import {
  ChartTable,
  type Bar,
  type ChartTableOptions,
  type IPrimitive,
  type PrimitiveHost,
  type PrimitiveRenderContext,
  type TableCell,
  type ZOrder,
} from "openalgo-charts";

export interface SeasonalityCell {
  text: string;
  bgColor?: string;
}

export interface SeasonalityTableOptions {
  position: string;
  cellWidth: number[];
  cellHeight: number;
  rowWeights: number[];
  widthPercent: number;
  heightPercent: number;
  fontSize: number;
  margin: number;
}

export interface SeasonalityTable {
  rows: SeasonalityCell[][];
  options: SeasonalityTableOptions;
}

export async function computeSeasonality(bars: Bar[], startYear = 2015): Promise<SeasonalityTable> {
  const resp = await fetch("/api/charts/profiles/seasonality", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: "seasonality", params: { start_year: startYear }, bars }),
  });
  if (!resp.ok) throw new Error(`seasonality failed: ${await resp.text()}`);
  const body = (await resp.json()) as { result: SeasonalityTable };
  return body.result;
}

/**
 * Pane primitive that draws the backend's table payload. The layout and
 * geometry are the library's ChartTable (the same primitive the reference
 * uses for seasonality — primitives/table.ts): rows of {text, bgColor} cells,
 * positioned/sized per the backend's options.
 */
export class SeasonalityPrimitive implements IPrimitive {
  private readonly _table: ChartTable;

  public constructor() {
    this._table = new ChartTable({ position: "bottom-center" });
  }

  public attached(host: PrimitiveHost): void { this._table.attached(host); }
  public detached(): void { this._table.detached(); }
  public zOrder(): ZOrder { return this._table.zOrder(); }

  public setData(result: SeasonalityTable): void {
    const o = result.options;
    this._table.setOptions({
      position: o.position as ChartTableOptions["position"],
      cellWidth: o.cellWidth,
      cellHeight: o.cellHeight,
      rowWeights: o.rowWeights,
      widthPercent: o.widthPercent,
      heightPercent: o.heightPercent,
      fontSize: o.fontSize,
      margin: o.margin,
    });
    this._table.setRows(result.rows as TableCell[][]);
  }

  public draw(ctx: CanvasRenderingContext2D, rc: PrimitiveRenderContext): void {
    this._table.draw(ctx, rc);
  }
}

export function createSeasonalityRenderer(): {
  primitive: IPrimitive;
  setData(result: unknown): void;
} {
  const primitive = new SeasonalityPrimitive();
  return {
    primitive,
    setData: (result: unknown) => primitive.setData(result as SeasonalityTable),
  };
}