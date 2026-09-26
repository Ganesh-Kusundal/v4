export type OrderSide = 'BUY' | 'SELL';
export type OrderType = 'MARKET' | 'LIMIT' | 'SL';
export type OrderMode = 'paper' | 'live' | 'replay' | 'unknown';

export type OrderIntent = {
  side: OrderSide;
  type: OrderType;
  price?: number | null;
  triggerPrice?: number | null;
  paneIndex: number;
};

export type OrderSafetyState = {
  armed: boolean;
  ready: boolean;
  replaying: boolean;
  mode: OrderMode;
  quantity: number;
  /** Live feed state from the server's FeedSupervisor; null when no supervisor. */
  feedState: string | null;
};

export const ORDER_VALIDATION_MESSAGES = {
  unarmed: 'Order rejected: arm trading first',
  notReady: 'Order rejected: feed not ready',
  replay: 'Order rejected: replay active',
  invalidQuantity: 'Order rejected: quantity must be a positive integer',
  pricePane: 'Order rejected: price pane only',
  missingPrice: 'Order rejected: missing price',
  unknownMode: 'Order rejected: mode unknown',
  latestQuote: 'Order rejected: latest quote unavailable',
  stopSide: 'Order rejected: invalid stop-side relationship',
} as const;

const isPositiveFinite = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value) && value > 0;

const isKnownMode = (mode: OrderMode): boolean =>
  mode === 'paper' || mode === 'live' || mode === 'replay';

const controlsReady = (state: OrderSafetyState): boolean =>
  state.ready && !state.replaying && isKnownMode(state.mode);

export function validateOrderIntent(
  intent: OrderIntent,
  state: OrderSafetyState,
  lastQuote: number | null,
): string | null {
  if (!state.armed) return ORDER_VALIDATION_MESSAGES.unarmed;
  if (state.replaying) return ORDER_VALIDATION_MESSAGES.replay;
  if (!state.ready) return ORDER_VALIDATION_MESSAGES.notReady;
  if (!isKnownMode(state.mode)) return ORDER_VALIDATION_MESSAGES.unknownMode;
  if (!Number.isInteger(state.quantity) || state.quantity < 1) {
    return ORDER_VALIDATION_MESSAGES.invalidQuantity;
  }
  if (intent.paneIndex !== 0) return ORDER_VALIDATION_MESSAGES.pricePane;

  const trigger = intent.type === 'MARKET' ? null : intent.triggerPrice ?? intent.price;
  if (intent.type !== 'MARKET' && !isPositiveFinite(trigger)) {
    return ORDER_VALIDATION_MESSAGES.missingPrice;
  }
  if (intent.type === 'SL') {
    if (!isPositiveFinite(lastQuote) || typeof trigger !== 'number' || !Number.isFinite(trigger)) {
      return ORDER_VALIDATION_MESSAGES.latestQuote;
    }
    if (intent.side === 'BUY' && !(trigger < lastQuote)) return ORDER_VALIDATION_MESSAGES.stopSide;
    if (intent.side === 'SELL' && !(trigger > lastQuote)) return ORDER_VALIDATION_MESSAGES.stopSide;
  }
  return null;
}

export interface OrderControlsOptions {
  initialState?: Partial<OrderSafetyState>;
  onStateChange?: (state: OrderSafetyState) => void;
}

export interface OrderControls {
  readonly state: OrderSafetyState;
  readonly lastQuote: number | null;
  readonly status: HTMLElement;
  setState(patch: Partial<OrderSafetyState>): void;
  setLastQuote(quote: number | null): void;
  dispose(): void;
}

const defaults: OrderSafetyState = {
  armed: false,
  ready: false,
  replaying: false,
  mode: 'unknown',
  quantity: 1,
  feedState: null,
};

const required = <T extends HTMLElement>(doc: Document, parent: HTMLElement, id: string, tag: keyof HTMLElementTagNameMap): T => {
  const existing = doc.getElementById(id);
  if (existing !== null) return existing as T;
  const node = doc.createElement(tag);
  node.id = id;
  parent.appendChild(node);
  return node as T;
};

const createRoot = (doc: Document, parent: HTMLElement): HTMLElement => {
  const existing = parent.querySelector<HTMLElement>('#order-controls');
  if (existing !== null) return existing;
  const root = doc.createElement('section');
  root.id = 'order-controls';
  parent.prepend(root);
  return root;
};

export function mountOrderControls(parent: HTMLElement, options: OrderControlsOptions = {}): OrderControls {
  const doc = parent.ownerDocument;
  const root = createRoot(doc, parent);
  if (root.children.length === 0) {
    const badge = doc.createElement('span');
    badge.id = 'order-product';
    badge.className = 'order-controls__badge';
    badge.textContent = 'INTRADAY';
    const quantityLabel = doc.createElement('label');
    quantityLabel.className = 'order-controls__label';
    quantityLabel.htmlFor = 'qty';
    quantityLabel.append(doc.createTextNode('Qty '));
    const quantity = doc.createElement('input');
    quantity.id = 'qty';
    quantity.type = 'number';
    quantity.min = '1';
    quantity.step = '1';
    quantity.value = '1';
    quantityLabel.appendChild(quantity);
    const armLabel = doc.createElement('label');
    armLabel.className = 'order-controls__label';
    const arm = doc.createElement('input');
    arm.id = 'arm';
    arm.type = 'checkbox';
    armLabel.append(arm, doc.createTextNode('Arm trading'));
    const mode = doc.createElement('span');
    mode.id = 'mode';
    mode.className = 'order-controls__mode';
    const feed = doc.createElement('span');
    feed.id = 'feed-status';
    feed.className = 'order-controls__feed';
    const status = doc.createElement('span');
    status.id = 'order-status';
    status.className = 'order-controls__status';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    const quote = doc.createElement('span');
    quote.id = 'order-quote';
    quote.className = 'order-controls__quote';
    root.append(badge, quantityLabel, armLabel, mode, feed, status, quote);
  }

  const arm = required<HTMLInputElement>(doc, root, 'arm', 'input');
  const quantity = required<HTMLInputElement>(doc, root, 'qty', 'input');
  const mode = required<HTMLElement>(doc, root, 'mode', 'span');
  const feed = required<HTMLElement>(doc, root, 'feed-status', 'span');
  const status = required<HTMLElement>(doc, root, 'order-status', 'span');
  const quote = required<HTMLElement>(doc, root, 'order-quote', 'span');
  if (arm.type !== 'checkbox') arm.type = 'checkbox';
  if (quantity.type !== 'number') quantity.type = 'number';
  quantity.min = '1';
  quantity.step = '1';
  arm.checked = false;
  quantity.value = '1';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');

  let state: OrderSafetyState = { ...defaults, ...options.initialState, armed: false };
  let lastQuote: number | null = null;
  let disposed = false;

  const render = (): void => {
    if (disposed) return;
    const enabled = controlsReady(state);
    const quantityValid = Number.isInteger(state.quantity) && state.quantity > 0;
    arm.checked = state.armed;
    arm.disabled = !enabled;
    if (Number.isFinite(state.quantity)) quantity.value = String(state.quantity);
    quantity.disabled = !enabled;
    const modeText = state.mode === 'unknown' ? 'MODE UNKNOWN' : state.mode.toUpperCase();
    mode.textContent = `${modeText} · ${state.ready && !state.replaying ? 'READY' : 'NOT READY'}`;
    feed.textContent = state.replaying
      ? 'Replay active'
      : state.feedState === 'resynchronizing'
        ? 'Feed reconnecting'
        : state.feedState === 'degraded'
          ? 'Feed stale'
          : state.feedState === 'halted'
            ? 'Feed halted'
            : state.ready
              ? 'Feed ready'
              : 'Feed not ready';
    quote.textContent = isPositiveFinite(lastQuote) ? `LTP ${lastQuote}` : 'LTP —';
    const reason = state.replaying
      ? 'Order entry disabled: replay active'
      : !state.ready
        ? 'Order entry disabled: feed not ready'
        : !isKnownMode(state.mode)
          ? 'Order entry disabled: mode unknown'
          : !state.armed
            ? 'Arm trading to enable order entry'
            : !quantityValid
              ? 'Order entry disabled: invalid quantity'
              : null;
    status.textContent = reason ?? 'Ready for guarded INTRADAY orders';
    status.dataset.state = state.replaying
      ? 'replay'
      : !enabled || !quantityValid
        ? 'blocked'
        : !state.armed
          ? 'disarmed'
          : 'ready';
  };

  const notify = (): void => {
    render();
    options.onStateChange?.({ ...state });
  };

  const onArm = (): void => {
    if (disposed) return;
    if (arm.checked && !controlsReady(state)) {
      arm.checked = false;
      return;
    }
    state = { ...state, armed: arm.checked };
    notify();
  };
  const onQuantity = (): void => {
    if (disposed) return;
    const value = Number(quantity.value);
    state = { ...state, quantity: Number.isFinite(value) ? value : Number.NaN };
    notify();
  };
  arm.addEventListener('change', onArm);
  quantity.addEventListener('input', onQuantity);
  quantity.addEventListener('change', onQuantity);
  render();

  return {
    get state() {
      return { ...state };
    },
    get lastQuote() {
      return lastQuote;
    },
    status,
    setState(patch: Partial<OrderSafetyState>): void {
      if (disposed) return;
      const next = { ...state, ...patch };
      state = controlsReady(next) ? next : { ...next, armed: false };
      notify();
    },
    setLastQuote(nextQuote: number | null): void {
      if (disposed) return;
      lastQuote = isPositiveFinite(nextQuote) ? nextQuote : null;
      render();
    },
    dispose(): void {
      if (disposed) return;
      disposed = true;
      arm.removeEventListener('change', onArm);
      quantity.removeEventListener('input', onQuantity);
      quantity.removeEventListener('change', onQuantity);
    },
  };
}
