import type { Widget } from 'openalgo-charts/widget';

/**
 * Order-quantity control, docked beside the replay bar. The widget's
 * context-menu onOrder hook carries no quantity, so every order entry
 * (context menu now, anything else later) reads the live input value at
 * place-time. No dialogs.
 */
let input: HTMLInputElement | null = null;

export function mountTradeBar(widget: Widget): void {
  const doc = widget.root.ownerDocument;
  const bar = doc.createElement('div');
  bar.className = 'v4-tradebar';
  bar.style.cssText =
    'display:flex;align-items:center;gap:6px;padding:6px 12px;border-top:1px solid var(--oac-bd);';

  const label = doc.createElement('span');
  label.textContent = 'Qty';
  label.style.cssText = 'font-size:12px;color:var(--oac-fg);';

  input = doc.createElement('input');
  input.type = 'number';
  input.min = '1';
  input.step = '1';
  input.value = '1';
  input.setAttribute('aria-label', 'Order quantity');
  input.style.cssText =
    'width:64px;padding:2px 6px;border:1px solid var(--oac-bd);border-radius:4px;background:transparent;color:var(--oac-fg);';

  bar.append(label, input);
  widget.root.appendChild(bar);
}

/** Clamped quantity for order placement: integer >= 1, default 1. */
export function getOrderQty(): number {
  const n = Math.floor(Number(input?.value));
  return Number.isFinite(n) && n >= 1 ? n : 1;
}
