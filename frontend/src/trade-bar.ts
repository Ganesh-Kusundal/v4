import type { Widget } from 'openalgo-charts/widget';
import type { Dock } from './dock';
import { getOrderQty as getShellbarQty } from './shellbar';

/**
 * Order-quantity and quick execution bar.
 * Controls are primarily hosted in the top Subbar (matching trading.png);
 * this module keeps backward compatibility for getOrderQty().
 */
export function mountTradeBar(_widget: Widget, _dock: Dock): void {
  // Quick trade controls are now prominently in the top subbar.
}

/** Clamped quantity for order placement: integer >= 1, default 1. */
export function getOrderQty(): number {
  return getShellbarQty();
}
