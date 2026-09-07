// Command palette (Ctrl+K) — search-driven access to all secondary actions.
// The shellbar keeps only essential controls; everything lives here.
export interface PaletteAction {
  id: string;
  label: string;
  category: string;
  execute: () => void;
}

export interface CommandPalette {
  open(): void;
  close(): void;
  isOpen(): boolean;
}

export function createCommandPalette(actions: PaletteAction[]): CommandPalette {
  let isOpen = false;
  let backdrop: HTMLDivElement | null = null;
  let input: HTMLInputElement | null = null;
  let resultsEl: HTMLDivElement | null = null;

  function build(): void {
    backdrop = document.createElement("div");
    backdrop.className = "palette-backdrop";
    backdrop.addEventListener("click", close);

    const modal = document.createElement("div");
    modal.className = "palette-modal";

    input = document.createElement("input");
    input.type = "text";
    input.className = "palette-input";
    input.placeholder = "Type a command…";
    input.addEventListener("input", render);
    input.addEventListener("keydown", onKey);

    resultsEl = document.createElement("div");
    resultsEl.className = "palette-results";

    modal.append(input, resultsEl);
    backdrop.append(modal);
    document.body.append(backdrop);
    render();
  }

  function render(): void {
    if (!resultsEl || !input) return;
    const q = input.value.trim().toLowerCase();
    const filtered = actions.filter(a =>
      !q || a.label.toLowerCase().includes(q) || a.category.toLowerCase().includes(q)
    );
    resultsEl.innerHTML = "";
    let lastCat = "";
    for (const a of filtered) {
      if (a.category !== lastCat) {
        const head = document.createElement("div");
        head.className = "palette-cat";
        head.textContent = a.category;
        resultsEl.append(head);
        lastCat = a.category;
      }
      const row = document.createElement("button");
      row.className = "palette-row";
      row.textContent = a.label;
      row.addEventListener("click", () => {
        close();
        a.execute();
      });
      resultsEl.append(row);
    }
    if (filtered.length === 0) {
      const empty = document.createElement("div");
      empty.className = "palette-empty";
      empty.textContent = "No results";
      resultsEl.append(empty);
    }
  }

  function onKey(e: KeyboardEvent): void {
    if (e.key === "Escape") { close(); }
    if (e.key === "Enter") {
      const rows = resultsEl?.querySelectorAll<HTMLButtonElement>(".palette-row");
      if (rows && rows.length > 0) rows[0].click();
    }
  }

  function open(): void {
    if (isOpen) return;
    build();
    isOpen = true;
    input?.focus();
  }

  function close(): void {
    if (!isOpen) return;
    backdrop?.remove();
    backdrop = null;
    input = null;
    resultsEl = null;
    isOpen = false;
  }

  return { open, close, isOpen: () => isOpen };
}
