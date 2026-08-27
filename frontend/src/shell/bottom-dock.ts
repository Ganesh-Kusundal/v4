// Bottom dock: tabbed host for scanner/backtest/orders/logs. Render-only host
// chrome; panels are injected by callers so bottom-dock stays generic. The
// dock starts collapsed (a single tab row); clicking a tab opens it, and the
// chevron collapses it again.
export type DockTab = "scanner" | "backtest" | "orders" | "logs";

export function createBottomDock(container: HTMLElement): {
  select(tab: DockTab): void;
  setContent(tab: DockTab, el: HTMLElement): void;
  toggle(open: boolean): void;
} {
  container.innerHTML = "";
  const tabs = document.createElement("div");
  tabs.id = "bottom-tabs";
  const panels = document.createElement("div");
  panels.id = "bottom-panels";

  const tabDefs: { id: DockTab; label: string }[] = [
    { id: "scanner", label: "Scanner" },
    { id: "backtest", label: "Backtest" },
    { id: "orders", label: "Orders" },
    { id: "logs", label: "Logs" },
  ];

  const panelMap = new Map<DockTab, HTMLElement>();
  for (const t of tabDefs) {
    const btn = document.createElement("button");
    btn.textContent = t.label;
    btn.dataset.tab = t.id;
    tabs.append(btn);
    const panel = document.createElement("div");
    panel.className = "dock-panel";
    panel.dataset.tab = t.id;
    panels.append(panel);
    panelMap.set(t.id, panel);
    btn.addEventListener("click", () => select(t.id));
  }

  // Chevron toggle sits after the tabs, far enough right to not crowd them.
  const toggleBtn = document.createElement("button");
  toggleBtn.className = "dock-toggle";
  toggleBtn.textContent = "▾";
  toggleBtn.title = "Collapse / expand panels";
  tabs.append(toggleBtn);

  function select(tab: DockTab): void {
    toggle(true);
    for (const b of Array.from(tabs.querySelectorAll<HTMLButtonElement>("[data-tab]"))) {
      b.classList.toggle("active", b.dataset.tab === tab);
    }
    for (const p of Array.from(panels.querySelectorAll<HTMLElement>(".dock-panel"))) {
      p.classList.toggle("active", p.dataset.tab === tab);
    }
  }

  function toggle(open: boolean): void {
    panels.classList.toggle("dock-hidden", !open);
    container.classList.toggle("dock-collapsed", !open);
    toggleBtn.textContent = open ? "▾" : "▸";
    toggleBtn.title = open ? "Collapse panels" : "Expand panels";
  }

  container.append(tabs, panels);
  toggleBtn.addEventListener("click", () => {
    const closed = panels.classList.contains("dock-hidden");
    toggle(closed); // closed -> open, open -> closed
  });
  toggle(false); // collapsible by default: only the tab row shows.

  return {
    select,
    setContent(tab, el) {
      const host = panelMap.get(tab);
      if (!host) return;
      host.innerHTML = "";
      host.append(el);
    },
    toggle,
  };
}
