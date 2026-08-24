// Indicator settings modal: host chrome bound to the library's IndicatorApi.
// Inputs are generated from the backend catalogue's param schema, so a new
// registry entry is editable here with zero JS changes. Apply calls
// IndicatorApi.setSettings(patch) — the Tier-2 fetch then recomputes on the
// backend with the new params (refetchOn includes every param name).
import type { IndicatorApi } from "openalgo-charts";
import type { CatalogueEntry } from "../backend-indicators";

interface ModalRefs {
  root: HTMLElement;
  title: HTMLElement;
  body: HTMLElement;
  ok: HTMLButtonElement;
  remove: HTMLButtonElement;
  cancel: HTMLButtonElement;
}

let refs: ModalRefs | null = null;
let active: { inst: IndicatorApi; entry: CatalogueEntry } | null = null;

function mount(): ModalRefs {
  if (refs) return refs;
  const root = document.getElementById("setmodal")!;
  refs = {
    root,
    title: document.getElementById("set-title")!,
    body: document.getElementById("set-body")!,
    ok: document.getElementById("set-ok") as HTMLButtonElement,
    remove: document.getElementById("set-remove") as HTMLButtonElement,
    cancel: document.getElementById("set-cancel") as HTMLButtonElement,
  };
  const close = () => hide();
  (document.getElementById("set-x") as HTMLButtonElement).addEventListener("click", close);
  refs.cancel.addEventListener("click", close);
  root.addEventListener("click", (e) => { if (e.target === root) close(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  return refs!;
}

export function showIndicatorModal(inst: IndicatorApi, entry: CatalogueEntry | undefined): void {
  const r = mount();
  active = { inst, entry: entry ?? { id: inst.indicatorId, name: inst.name, category: "", placement: "pane", params: [], plots: [] } };
  r.title.textContent = `${inst.name} settings`;

  r.body.innerHTML = "";
  // Routing rows are read-only context (Tier-2 refetchOn keys), shown so the
  // trader can see which series the instance computes against. Editing them
  // happens through symbol/interval controls, not here.
  const routing = ["symbol", "exchange", "interval"] as const;
  for (const key of routing) {
    const cur = inst.settings()[key];
    if (typeof cur !== "string" || !cur) continue;
    const row = document.createElement("div");
    row.className = "set-row";
    const label = document.createElement("label");
    label.textContent = key;
    const ctl = document.createElement("div");
    ctl.className = "set-ctl";
    const input = document.createElement("input");
    input.type = "text";
    input.value = cur;
    input.disabled = true;
    ctl.append(input);
    row.append(label, ctl);
    r.body.append(row);
  }

  const params = active.entry.params ?? [];
  for (const p of params) {
    const row = document.createElement("div");
    row.className = "set-row";
    const label = document.createElement("label");
    label.textContent = p.name.replace(/_/g, " ");
    label.htmlFor = `set-${p.name}`;
    const ctl = document.createElement("div");
    ctl.className = "set-ctl";
    const input = document.createElement("input");
    input.id = `set-${p.name}`;
    input.type = "number";
    input.step = p.type === "int" ? "1" : "any";
    const current = inst.settings()[p.name];
    input.value = String(typeof current === "number" ? current : p.default);
    input.dataset.param = p.name;
    input.dataset.kind = p.type === "int" ? "int" : "float";
    ctl.append(input);
    row.append(label, ctl);
    r.body.append(row);
  }
  if (params.length === 0) {
    const note = document.createElement("div");
    note.className = "hint";
    note.textContent = "This indicator takes no parameters.";
    r.body.append(note);
  }

  r.remove.hidden = false;
  r.remove.onclick = () => { inst.remove(); hide(); };
  r.ok.onclick = () => {
    const patch: Record<string, number> = {};
    for (const input of Array.from(r.body.querySelectorAll<HTMLInputElement>("input[data-param]"))) {
      const raw = input.value.trim();
      if (raw === "") continue;
      patch[input.dataset.param!] = input.dataset.kind === "int" ? Math.round(Number(raw)) : Number(raw);
    }
    if (Object.keys(patch).length > 0) inst.setSettings(patch as never);
    hide();
  };
  r.root.hidden = false;
}

export function hideIndicatorModal(): void {
  if (refs) refs.root.hidden = true;
  active = null;
}
function hide(): void { hideIndicatorModal(); }
