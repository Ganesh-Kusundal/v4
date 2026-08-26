// Keyboard shortcuts: ShortcutManager maps keys to commands; this module
// routes resolved commands onto the shell's existing chart actions. Pure
// chrome — the library owns key→command, the host owns command→chart-op.
import { ShortcutManager, DEFAULT_KEYMAP, type ShortcutScope } from "openalgo-charts";

export interface ShortcutActions {
  panLeft(): void; panRight(): void;
  panLeftFast(): void; panRightFast(): void;
  panUp(): void; panDown(): void;
  zoomIn(): void; zoomOut(): void;
  resetScale(): void; fitContent(): void;
  screenshot(): void;
  toggleGridVert(): void; toggleGridHorz(): void;
  toggleCrosshairMagnet(): void;
  [command: string]: (() => void) | undefined;
}

export interface ShortcutShell {
  manager: ShortcutManager;
  list(): { command: string; label: string; combos: string[] }[];
  start(): () => void;
}

/**
 * Build the shell's shortcut layer. Default scope is `global` — this is a
 * terminal shell, not a single-chart page: the keymap should act whether the
 * pointer is over the chart or a panel. `hover` (the library default) only
 * fires over the chart, which leaves the shellbar/panels dead on the keyboard.
 *
 * The chart is created with `shortcuts: false` so this handler is the single
 * owner of key routing (otherwise the chart's own document-level keydown fires
 * on top of ours and every key acts twice).
 */
export function createShellShortcuts(
  actions: ShortcutActions,
  opts: { scope?: ShortcutScope } = {},
): ShortcutShell {
  const manager = new ShortcutManager({ scope: opts.scope ?? "global", persist: false });
  const off = manager.on((e) => {
    const fn = actions[e.command];
    if (fn) fn();
  });
  return {
    manager,
    list: () => DEFAULT_KEYMAP.map((e) => ({ command: e.command, label: e.label, combos: [...e.combos] })),
    start: () => {
      const handler = (ev: KeyboardEvent) => {
        // Never hijack typing in inputs/selects/contentEditable (symbol search,
        // bracket ticket, indicator filter). Library helper, not our logic.
        if (ShortcutManager.shouldIgnore(ev.target)) return;
        const cmd = manager.resolve(ev);
        if (cmd) { ev.preventDefault(); actions[cmd]?.(); }
      };
      window.addEventListener("keydown", handler);
      return () => { window.removeEventListener("keydown", handler); off(); };
    },
  };
}

export { DEFAULT_KEYMAP };