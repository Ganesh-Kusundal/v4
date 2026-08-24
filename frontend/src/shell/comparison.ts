// Compare overlay: second instrument on the price pane via the library's
// comparisonController. Bars come from the SAME Tradex history endpoint as the
// primary chart, so alignment is against our own datalake/broker feed.
import { addComparison, comparisonController, type ComparisonHandle } from "openalgo-charts";

type Feed = { getBars(req: { symbol: string; exchange: string; interval: string }): Promise<{ time: number }[]> };

const PALETTE = ["#f0b90b", "#e15fdf", "#4dd0e1", "#9ccc65"];

export function wireCompare(
  chart: unknown,
  feed: Feed,
  getState: () => { exchange: string; symbol: string; interval: string },
): void {
  const modal = document.getElementById("cmpmodal")!;
  const listEl = document.getElementById("cmp-list")!;
  const symInput = document.getElementById("cmp-sym") as HTMLInputElement;
  const modeSel = document.getElementById("cmp-mode") as HTMLSelectElement | null;
  if (!modal || !listEl || !symInput || !modeSel) return;

  // ponytail: one controller instance per page is all the tier supports well.
  const ctrl = comparisonController(chart as never, { mode: modeSel.value as never });

  function renderList(): void {
    listEl.innerHTML = "";
    for (const h of ctrl.list() as readonly ComparisonHandle[]) {
      const row = document.createElement("div");
      row.className = "cmp-row";
      row.innerHTML = `<span class="sw"></span><b>${h.symbol}</b>`;
      const del = document.createElement("button");
      del.className = "cmp-del";
      del.textContent = "×";
      del.title = "Remove";
      del.addEventListener("click", () => { ctrl.remove(h); renderList(); });
      row.append(del);
      listEl.append(row);
    }
  }

  async function add(): Promise<void> {
    const sym = symInput.value.trim().toUpperCase();
    if (!sym) return;
    const st = getState();
    try {
      const bars = await feed.getBars({ symbol: sym, exchange: st.exchange, interval: st.interval });
      if (bars.length === 0) { symInput.placeholder = `${sym}: no data`; return; }
      const color = PALETTE[ctrl.list().length % PALETTE.length];
      addComparison(chart as never, { symbol: sym, bars: bars as never, color });
      renderList();
      symInput.value = "";
    } catch (e) {
      symInput.placeholder = e instanceof Error ? e.message.slice(0, 40) : "fetch failed";
    }
  }

  (document.getElementById("cmp-add") as HTMLButtonElement).addEventListener("click", () => void add());
  symInput.addEventListener("keydown", (e) => { if (e.key === "Enter") void add(); });
  (document.getElementById("cmp-close") as HTMLButtonElement).addEventListener("click", hide);
  (document.getElementById("cmp-x") as HTMLButtonElement).addEventListener("click", hide);
  modeSel.addEventListener("change", () => ctrl.setMode(modeSel.value as never));
  modal.addEventListener("click", (e) => { if (e.target === modal) hide(); });

  function hide(): void { modal.hidden = true; }

  // Public toggle for the shellbar Compare button.
  document.addEventListener("tradex:compare", () => {
    modal.hidden = !modal.hidden;
    if (!modal.hidden) { renderList(); symInput.focus(); }
  });
}

export function toggleCompareUi(): void {
  document.dispatchEvent(new CustomEvent("tradex:compare"));
}
