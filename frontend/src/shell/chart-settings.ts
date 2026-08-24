// Chart settings dialog: generic renderer over the library's own
// chartSettingsSchema(chart). No control is invented here — every row maps to
// a schema input the engine consumes via applyChartSettings. Color-pair rows
// follow master CLAUDE.md grammar: one labelled row, two swatches.
import {
  applyChartSettings,
  chartSettingsSchema,
  readChartSettings,
  type ChartSettingsTab,
} from "openalgo-charts";

// ChartSettingsInput is not a public export; this structural copy matches the
// schema's documented shape (base inputs + the paired-colour variant).
interface SchemaInput {
  key: string; label: string; type: string; group?: string; default?: unknown;
  enabled?: { key: string; default: boolean };
  up?: { key: string; label: string; default: string };
  down?: { key: string; label: string; default: string };
}

type Values = Record<string, string | number | boolean>;

export function wireChartSettings(chart: unknown): void {
  const modal = document.getElementById("chartset")!;
  const tabsEl = document.getElementById("cset-tabs")!;
  const bodyEl = document.getElementById("cset-body")!;
  if (!modal || !tabsEl || !bodyEl) return;

  let schema: ChartSettingsTab[] = [];
  let values: Values = {};
  let dirty: Values = {};
  let activeTab = "";

  function render(): void {
    const tab = schema.find((t) => t.id === activeTab) ?? schema[0];
    if (!tab) return;
    for (const b of Array.from(tabsEl.children)) {
      b.classList.toggle("is-on", (b as HTMLElement).dataset.tab === tab.id);
    }
    bodyEl.innerHTML = "";
    let group = "";
    for (const input of tab.inputs as SchemaInput[]) {
      const g = (input as { group?: string }).group ?? "";
      if (g && g !== group) {
        const h = document.createElement("div");
        h.className = "set-group";
        h.textContent = g;
        bodyEl.append(h);
      }
      group = g;
      bodyEl.append(row(tab.id, input));
    }
  }

  function row(_tabId: string, input: SchemaInput): HTMLElement {
    const i = input;

    // Paired colour row: [x] Label [up][down]
    if (i.type === "colorPair" && i.up && i.down) {
      const r = document.createElement("div");
      r.className = "set-row";
      const swSlot = document.createElement("div");
      swSlot.className = "set-sw";
      const enableKey = i.enabled?.key;
      const chk = document.createElement("input");
      chk.type = "checkbox";
      chk.checked = Boolean(values[enableKey ?? ""] ?? true);
      chk.addEventListener("change", () => {
        if (enableKey) dirty[enableKey] = chk.checked;
      });
      swSlot.append(chk);
      const label = document.createElement("label");
      label.textContent = i.label;
      const ctl = document.createElement("div");
      ctl.className = "set-ctl";
      for (const side of [i.up, i.down]) {
        const c = document.createElement("input");
        c.type = "color";
        c.value = String(values[side.key] ?? side.default);
        c.style.width = "26px"; c.style.height = "24px";
        c.addEventListener("input", () => { dirty[side.key] = c.value; });
        ctl.append(c);
      }
      r.append(swSlot, label, ctl);
      return r;
    }

    const r = document.createElement("div");
    r.className = "set-row";
    const label = document.createElement("label");
    label.textContent = i.label;
    const ctl = document.createElement("div");
    ctl.className = "set-ctl";
    const cur = values[i.key];
    if (i.type === "boolean") {
      const c = document.createElement("input");
      c.type = "checkbox";
      c.checked = Boolean(cur);
      c.addEventListener("change", () => { dirty[i.key] = c.checked; });
      ctl.append(c);
    } else {
      const inp = document.createElement("input");
      inp.type = i.type === "number" ? "number" : i.type === "color" ? "color" : "text";
      if (cur !== undefined) inp.value = String(cur);
      inp.addEventListener("change", () => {
        dirty[i.key] = i.type === "number" ? Number(inp.value) : inp.value;
      });
      ctl.append(inp);
    }
    r.append(label, ctl);
    return r;
  }

  function buildTabs(): void {
    tabsEl.innerHTML = "";
    for (const t of schema) {
      const b = document.createElement("button");
      b.className = "cset-tab";
      b.dataset.tab = t.id;
      b.textContent = t.label;
      b.addEventListener("click", () => { activeTab = t.id; render(); });
      tabsEl.append(b);
    }
  }

  function open(): void {
    schema = chartSettingsSchema(chart as never);
    values = readChartSettings(chart as never) as Values;
    dirty = {};
    activeTab = schema[0]?.id ?? "";
    buildTabs();
    render();
    modal.hidden = false;
  }
  function hide(): void { modal.hidden = true; }

  (document.getElementById("cset-x") as HTMLButtonElement).addEventListener("click", hide);
  (document.getElementById("cset-cancel") as HTMLButtonElement).addEventListener("click", hide);
  (document.getElementById("cset-ok") as HTMLButtonElement).addEventListener("click", () => {
    if (Object.keys(dirty).length > 0) applyChartSettings(chart as never, dirty);
    hide();
  });
  (document.getElementById("cset-defaults") as HTMLButtonElement).addEventListener("click", () => {
    const tab = schema.find((t) => t.id === activeTab);
    if (!tab) return;
    const defaults: Values = {};
    for (const i of tab.inputs as SchemaInput[]) {
      if (i.type === "colorPair") {
        if (i.up) defaults[i.up.key] = i.up.default;
        if (i.down) defaults[i.down.key] = i.down.default;
      } else if (i.default !== undefined) {
        defaults[i.key] = i.default as Values[string];
      }
    }
    dirty = { ...dirty, ...defaults };
    render();
  });
  modal.addEventListener("click", (e) => { if (e.target === modal) hide(); });

  document.addEventListener("tradex:chartsettings", open);
}

export function toggleChartSettingsUi(): void {
  document.dispatchEvent(new CustomEvent("tradex:chartsettings"));
}
