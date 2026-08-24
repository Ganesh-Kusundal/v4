// Draw rail: host chrome for the openalgo-charts/draw tier. The controller
// owns placement/drag/undo; this file only renders tool buttons and binds
// them to setTool(). No drawing math lives here.
import {
  DrawingController,
  registeredDrawingTools,
  getDrawingTool,
} from "openalgo-charts/draw";

interface RailButton { el: HTMLButtonElement; toolId: string | null }

export interface DrawRailApi {
  setActive(toolId: string | null): void;
  refreshHistory(): void;
}

const TOOL_ORDER: readonly string[] = [
  "trend-line",
  "ray",
  "extended-line",
  "horizontal-line",
  "horizontal-ray",
  "vertical-line",
  "cross-line",
  "rectangle",
  "ellipse",
  "parallel-channel",
  "fib-retracement",
  "fib-extension",
  "measure",
  "long-position",
  "short-position",
  "highlighter",
];

function svgIcon(toolId: string): string {
  // Minimal geometric glyphs keyed by tool family; the library ships no DOM
  // or icons, so the host draws its own. Plain strokes, no emoji.
  switch (toolId) {
    case "trend-line": return '<svg viewBox="0 0 24 24"><path d="M4 20 L20 4"/></svg>';
    case "ray": return '<svg viewBox="0 0 24 24"><path d="M4 18 L14 8 M14 8 L11 7 M14 8 L13 11"/></svg>';
    case "extended-line": return '<svg viewBox="0 0 24 24"><path d="M2 19 L22 5"/></svg>';
    case "horizontal-line": return '<svg viewBox="0 0 24 24"><path d="M3 12 L21 12"/></svg>';
    case "horizontal-ray": return '<svg viewBox="0 0 24 24"><path d="M6 12 L21 12"/></svg>';
    case "vertical-line": return '<svg viewBox="0 0 24 24"><path d="M12 3 L12 21"/></svg>';
    case "cross-line": return '<svg viewBox="0 0 24 24"><path d="M12 3 L12 21 M3 12 L21 12"/></svg>';
    case "rectangle": return '<svg viewBox="0 0 24 24"><rect x="4" y="6" width="16" height="12" rx="1"/></svg>';
    case "ellipse": return '<svg viewBox="0 0 24 24"><ellipse cx="12" cy="12" rx="8" ry="6"/></svg>';
    case "parallel-channel": return '<svg viewBox="0 0 24 24"><path d="M4 16 L18 6 M4 20 L18 10"/></svg>';
    case "fib-retracement": return '<svg viewBox="0 0 24 24"><path d="M4 6 L20 6 M4 12 L20 12 M4 18 L20 18"/></svg>';
    case "fib-extension": return '<svg viewBox="0 0 24 24"><path d="M4 4 L20 4 M4 10 L20 10 M4 16 L20 16"/></svg>';
    case "measure": return '<svg viewBox="0 0 24 24"><path d="M4 12 H20 M4 9 v6 M20 9 v6"/></svg>';
    case "long-position": return '<svg viewBox="0 0 24 24"><path d="M4 18 L20 18 M12 18 V8 M8 12 L12 8 L16 12"/></svg>';
    case "short-position": return '<svg viewBox="0 0 24 24"><path d="M4 6 L20 6 M12 6 V16 M8 12 L12 16 L16 12"/></svg>';
    case "highlighter": return '<svg viewBox="0 0 24 24"><path d="M4 17 C8 10, 16 10, 20 15" stroke-width="3"/></svg>';
    default: return '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="7"/></svg>';
  }
}

export function createDrawRail(
  container: HTMLElement,
  chart: unknown,
  opts: { magnetCheckbox: HTMLInputElement | null },
): DrawRailApi | null {
  // Import side effect registers builtin tools; guard anyway so a slim bundle
  // degrades to no rail instead of a dead button (never ship inert controls).
  let controller: InstanceType<typeof DrawingController> | null = null;
  try {
    const Ctor = DrawingController as unknown as new (
      c: unknown,
      o: Record<string, unknown>,
    ) => InstanceType<typeof DrawingController>;
    controller = new Ctor(chart, { magnet: opts.magnetCheckbox?.checked ?? false });
  } catch {
    container.remove();
    return null;
  }

  // registeredDrawingTools is a function returning the registered tool list.
  let available = new Set<string>();
  try {
    available = new Set(
      ((registeredDrawingTools as unknown as () => { id: string }[])()).map((t) => t.id),
    );
  } catch {
    // Tier loaded but registry unreadable: fall back to the curated list and
    // let setTool() throw loudly per-tool rather than hiding everything.
    available = new Set(TOOL_ORDER);
  }

  container.innerHTML = "";
  const buttons: RailButton[] = [];

  function makeBtn(toolId: string | null, label: string, onClick: () => void, icon?: string): RailButton {
    const b = document.createElement("button");
    b.type = "button";
    if (toolId !== null) {
      b.innerHTML = icon ?? svgIcon(toolId);
      const tip = document.createElement("span");
      tip.className = "railtip";
      const name = toolId === null ? "Cursor" : safeName(toolId);
      tip.innerHTML = `${name}${SHORTCUTS[toolId] ? `<b>${SHORTCUTS[toolId]}</b>` : ""}`;
      b.append(tip);
    } else {
      b.textContent = label;
    }
    b.addEventListener("click", onClick);
    container.append(b);
    return { el: b, toolId };
  }

  function safeName(id: string): string {
    try { return (getDrawingTool as unknown as (i: string) => { name: string })(id).name; }
    catch { return id; }
  }

  function select(toolId: string | null): void {
    controller!.setTool(toolId);
    for (const { el, toolId: tid } of buttons) {
      el.classList.toggle("is-on", toolId === null ? tid === null : tid === toolId);
    }
  }

  // Cursor (disarm) first.
  const cursorBtn = makeBtn(null, "✛", () => select(null), '<svg viewBox="0 0 24 24"><path d="M6 3 L6 17 L10 13 L13 20 L15 19 L12 12 L18 12 Z"/></svg>');
  cursorBtn.el.title = "Cursor";
  cursorBtn.el.classList.add("is-on");
  buttons.push(cursorBtn);

  for (const toolId of TOOL_ORDER) {
    if (!available.has(toolId)) continue;
    const btn = makeBtn(toolId, "", () => select(toolId));
    buttons.push(btn);
  }
  // Any registered tool the fixed list does not cover still gets a button,
  // so a future tier addition is usable without editing this file.
  for (const id of available) {
    if (TOOL_ORDER.includes(id)) continue;
    buttons.push(makeBtn(id, "", () => select(id)));
  }

  container.append(Object.assign(document.createElement("span"), { className: "sep" }));

  const undo = makeBtn(null, "↶", () => { controller!.undo(); }, '<svg viewBox="0 0 24 24"><path d="M9 14 L4 9 L9 4"/><path d="M4 9 H15 a5 5 0 0 1 0 10 H8"/></svg>');
  undo.el.title = "Undo";
  const redo = makeBtn(null, "↷", () => { controller!.redo(); }, '<svg viewBox="0 0 24 24"><path d="M15 14 L20 9 L15 4"/><path d="M20 9 H9 a5 5 0 0 0 0 10 h7"/></svg>');
  redo.el.title = "Redo";
  const del = makeBtn(null, "Del", () => {
    const sel = (controller as unknown as { selected(): string | null }).selected();
    if (sel) controller!.remove(sel);
  });
  del.el.title = "Delete selected";
  const clear = makeBtn(null, "Clr", () => { controller!.clear(); });
  clear.el.title = "Remove all drawings";

  if (opts.magnetCheckbox) {
    opts.magnetCheckbox.addEventListener("change", () => {
      controller!.setOptions({ magnet: opts.magnetCheckbox!.checked } as never);
    });
  }

  return {
    setActive(toolId) { select(toolId); },
    refreshHistory() { /* undo/redo read live state */ },
  };
}

const SHORTCUTS: Record<string, string> = {
  "trend-line": "Alt+T",
  "horizontal-line": "Alt+H",
  "vertical-line": "Alt+V",
  "cross-line": "Alt+C",
};
