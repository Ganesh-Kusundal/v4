// Watchlist shell: universe-backed search + selection. Host renders
// its own DOM so the chart stays render-only; selection drives
// DataFeed via chart-level symbol change.
import { expectJson } from "../http";
export interface WatchItem { symbol: string; exchange: string; }

export function createWatchlist(
  container: HTMLElement,
  onSelect: (item: WatchItem) => void,
  initial: WatchItem,
): { setActive(item: WatchItem): void } {
  container.innerHTML = "";
  const head = document.createElement("div");
  head.className = "wl-head";
  const title = document.createElement("h3");
  title.textContent = "Watchlist";
  const search = document.createElement("input");
  search.type = "search";
  search.placeholder = "Search (Nifty 500)";
  search.className = "wl-search";
  head.append(title, search);
  const list = document.createElement("div");
  list.className = "wl-list";
  container.append(head, list);

  let activeKey = `${initial.exchange}:${initial.symbol}`;
  let all: WatchItem[] = [];
  let timer: number | null = null;

  function row(item: WatchItem): HTMLElement {
    const key = `${item.exchange}:${item.symbol}`;
    const el = document.createElement("div");
    el.className = "wl-row" + (key === activeKey ? " active" : "");
    el.dataset.key = key;
    const left = document.createElement("span");
    left.innerHTML = `<span class="wl-sym">${item.symbol}</span><span class="wl-exch">${item.exchange}</span>`;
    const right = document.createElement("span");
    right.className = "wl-meta";
    right.textContent = "";
    el.append(left, right);
    el.addEventListener("click", () => onSelect(item));
    return el;
  }

  function render(items: WatchItem[]): void {
    list.innerHTML = "";
    for (const it of items.slice(0, 200)) list.append(row(it));
  }

  async function load(q: string): Promise<void> {
    const params = new URLSearchParams({ universe: "nifty500" });
    if (q.trim()) params.set("q", q.trim());
    try {
      const resp = await fetch(`/api/charts/symbols?${params}`);
      const body = await expectJson<{ symbols: WatchItem[] }>(resp);
      all = body.symbols;
      render(all);
    } catch {
      // fallback: show active only
      render([initial]);
    }
  }

  search.addEventListener("input", () => {
    if (timer !== null) clearTimeout(timer);
    timer = window.setTimeout(() => { void load(search.value); }, 250);
  });

  void load("");

  return {
    setActive(item: WatchItem) {
      activeKey = `${item.exchange}:${item.symbol}`;
      for (const el of Array.from(list.querySelectorAll<HTMLElement>(".wl-row"))) {
        el.classList.toggle("active", el.dataset.key === activeKey);
      }
    },
  };
}
