/**
 * Order engine (ARCHITECTURE.md §9.5): the chart-trading write path. Drives the
 * order state machine with: client-token idempotency, an arm/confirm gate,
 * pre-trade validation, rate-limited drag-modify, OCO linking, and analyzer
 * (sandbox) mode. Network-agnostic: it talks to an injected OrderFeed (the
 * FakeBroker simulates it in tests/demos).
 *
 * Two lifecycles are tracked per order and they are deliberately not merged:
 *
 *   state         the historical `ClientOrderState`. Existing consumers read it
 *                 and its meaning is unchanged, including the parts that are
 *                 optimistic (a resolved place() reads `working`).
 *   intent        what THIS client actually knows: submitted, ambiguous,
 *                 acknowledged, settled. A transport result never reaches past
 *                 SUBMITTED.
 *   brokerStatus  what the BROKER said. Undefined until an authoritative event
 *                 arrives, and written by nothing else.
 *
 * A resolved promise is not an order: it says the request left and an answer
 * came back, not that the exchange has anything. The v2 broker contract replaces
 * `state` outright with the two-field `OrderRow`; removing it here would break
 * every consumer of `ClientOrderState`, so the honest fields are added alongside
 * and the merge is deferred to v2.
 */
import { transition, isTerminal, type ClientOrderState, type OrderEvent } from './order-state-machine';
import { validateOrder, type OrderConstraints, type ValidationResult } from './validation';
import type { OrderSide, OrderStatus, OrderType } from './types';

export interface PlaceRequest {
  symbol: string;
  exchange?: string;
  side: OrderSide;
  type: OrderType;
  qty: number;
  price?: number;
  triggerPrice?: number;
  /** Product: CNC (delivery), NRML (F&O carry), MIS (intraday). Required by OpenAlgo. */
  product?: 'CNC' | 'NRML' | 'MIS';
  /** Idempotency token; a retry with the same token is never double-sent. */
  clientToken?: string;
}

/** Fields a modify may change. Whole-order feeds fill the rest from their cache. */
export interface ModifyPatch {
  price?: number;
  triggerPrice?: number;
  qty?: number;
}

/**
 * The minimal broker write interface the `OrderEngine` drives: place / modify /
 * cancel. `OpenAlgoTradeFeed` implements this. Distinct from the base-tier
 * `TradeFeed` (a higher-level place + subscribe shape in `openalgo-charts`):
 * implement `OrderFeed` for the engine's write path.
 */
export interface OrderFeed {
  place(req: PlaceRequest & { mode: TradeMode }): Promise<{ orderId: string }>;
  modify(orderId: string, patch: ModifyPatch): Promise<void>;
  cancel(orderId: string): Promise<void>;
}

export type TradeMode = 'live' | 'analyzer';
export type GateFn = (req: PlaceRequest) => boolean | Promise<boolean>;

/**
 * Client-owned lifecycle of an intent, kept apart from the broker's own view.
 *
 *   BLOCKED            never sent: validation failed, the gate declined, or the
 *                      feed proved the request never left
 *   SUBMITTING         in flight
 *   SUBMITTED          transport returned success. NOT an order yet.
 *   AMBIGUOUS          transport failed, or a fresh book does not mention it.
 *                      May or may not be live at the exchange. Absorbing until
 *                      the broker speaks.
 *   ACKNOWLEDGED       the broker has accounted for it
 *   MODIFY_SUBMITTING  a modify is in flight
 *   CANCEL_SUBMITTING  a cancel is in flight
 *   RECONCILING        our picture may be behind; a snapshot is being fetched
 *   SETTLED            the broker reported a final state
 */
export type IntentState =
  | 'BLOCKED'
  | 'SUBMITTING'
  | 'SUBMITTED'
  | 'AMBIGUOUS'
  | 'ACKNOWLEDGED'
  | 'MODIFY_SUBMITTING'
  | 'CANCEL_SUBMITTING'
  | 'RECONCILING'
  | 'SETTLED';

/**
 * What a feed throws when the request PROVABLY never left: it failed before the
 * socket was written (bad arguments, no context cached, offline, DNS). Only this
 * marker releases an idempotency token, because only this proves there is
 * nothing live to double up on. See the catch in `placeOrder`.
 */
export interface PreflightFailure {
  readonly preflight: true;
}

/** True when a thrown value declares itself a pre-flight failure. */
export function isPreflightFailure(err: unknown): boolean {
  return typeof err === 'object' && err !== null && (err as { preflight?: unknown }).preflight === true;
}

/** Extra fields for the one-click market order, which a chart button cannot otherwise set. */
export interface MarketOrderOptions {
  exchange?: string;
  product?: 'CNC' | 'NRML' | 'MIS';
  /** Idempotency token, so a double-clicked button places one order, not two. */
  clientToken?: string;
}

export interface ModifyOptions {
  /**
   * Explicit stop trigger. Omitted, the trigger follows the order type: SL-M
   * treats the dragged level as the trigger, SL carries its trigger at the
   * offset the order was placed with, and the rest have none.
   */
  triggerPrice?: number;
}

export interface OrderEngineOptions {
  feed: OrderFeed;
  constraints: OrderConstraints;
  mode?: TradeMode;
  /** Armed = fire immediately; otherwise the gate must approve each order. */
  armed?: boolean;
  gate?: GateFn;
  minModifyIntervalMs?: number;
  now?: () => number;
  idGen?: () => string;
  /** Called when a drag-modify price fails validation (so the UI can snap back). */
  onValidationError?: (reason: string) => void;
  /**
   * How many settled orders stay readable before the oldest are dropped. A
   * trading session is a long-lived page and the per-order maps used to grow
   * for its whole life. 0 drops each order the moment it settles.
   */
  maxSettledOrders?: number;
}

export interface PlaceResult {
  ok: boolean;
  clientId?: string;
  state?: ClientOrderState;
  /** Client-owned intent. `ok: true` means SUBMITTED, never acknowledged. */
  intent?: IntentState;
  reason?: string;
}

interface Tracked {
  clientId: string;
  state: ClientOrderState;
  intent: IntentState;
  /** Last state the BROKER reported. Undefined until it reports one. */
  brokerStatus?: OrderStatus;
  brokerId?: string;
  req: PlaceRequest;
  ocoPeer?: string;
  /** Counted once into the settled ring, so a repeated terminal event cannot double-count. */
  pruned?: boolean;
}

type PatchResult = { ok: true; patch: ModifyPatch } | { ok: false; reason: string };

/** How a broker-reported status drives the client machine. `pending` adds nothing. */
const BROKER_EVENT: Readonly<Record<OrderStatus, OrderEvent | undefined>> = {
  pending: undefined,
  working: 'ack',
  partial: 'partialFill',
  filled: 'fill',
  cancelled: 'cancelled',
  rejected: 'reject',
};

const BROKER_FINAL: ReadonlySet<OrderStatus> = new Set<OrderStatus>(['filled', 'cancelled', 'rejected']);

const DEFAULT_MAX_SETTLED = 500;

export class OrderEngine {
  private readonly _feed: OrderFeed;
  private readonly _constraints: OrderConstraints;
  private readonly _mode: TradeMode;
  private readonly _armed: boolean;
  private readonly _gate?: GateFn;
  private readonly _minModifyMs: number;
  private readonly _now: () => number;
  private readonly _idGen: () => string;
  private readonly _onValidationError?: (reason: string) => void;
  private readonly _maxSettled: number;

  private readonly _orders = new Map<string, Tracked>();
  private readonly _byBroker = new Map<string, string>();
  private readonly _sentTokens = new Set<string>();
  private readonly _lastModifyAt = new Map<string, number>();
  private readonly _pendingModify = new Map<string, ModifyPatch>();
  /** Settled client ids, oldest first: the eviction order for the maps above. */
  private readonly _settledIds: string[] = [];
  private _counter = 0;

  public constructor(opts: OrderEngineOptions) {
    this._feed = opts.feed;
    this._constraints = opts.constraints;
    this._mode = opts.mode ?? 'live';
    this._armed = opts.armed ?? false;
    this._gate = opts.gate;
    this._minModifyMs = opts.minModifyIntervalMs ?? 150;
    this._now = opts.now ?? (() => (typeof performance !== 'undefined' ? performance.now() : 0));
    this._idGen = opts.idGen ?? (() => `c${++this._counter}`);
    this._onValidationError = opts.onValidationError;
    this._maxSettled = Math.max(0, opts.maxSettledOrders ?? DEFAULT_MAX_SETTLED);
  }

  public get mode(): TradeMode { return this._mode; }
  public state(clientId: string): ClientOrderState | undefined { return this._orders.get(clientId)?.state; }

  /** Client-owned intent: what we did, as opposed to what the broker confirmed. */
  public intentState(clientId: string): IntentState | undefined { return this._orders.get(clientId)?.intent; }

  /**
   * The broker's own last word. Undefined means the broker has said nothing
   * about this order yet, however far `state` has advanced on transport results.
   */
  public brokerStatus(clientId: string): OrderStatus | undefined { return this._orders.get(clientId)?.brokerStatus; }

  public async placeOrder(req: PlaceRequest): Promise<PlaceResult> {
    // Quantity constraints (freeze, lot grid) bind on EVERY order type; only the
    // price checks are conditional, because a market order has no price. Gating
    // the whole validate call on `price !== undefined` left the market order, the
    // one that cannot be taken back, as the single unchecked path.
    const v: ValidationResult = validateOrder(req.price, req.qty, this._constraints);
    if (!v.ok) return { ok: false, reason: v.reason, intent: 'BLOCKED' };
    const snappedPrice = req.price === undefined ? undefined : v.price;

    // A stop trigger is a price and earns the same tick snap and band check.
    let snappedTrigger = req.triggerPrice;
    if (req.triggerPrice !== undefined) {
      const t = validateOrder(req.triggerPrice, req.qty, this._constraints);
      if (!t.ok) return { ok: false, reason: `trigger: ${t.reason}`, intent: 'BLOCKED' };
      snappedTrigger = t.price;
    }

    const token = req.clientToken ?? this._idGen();
    // Claim the token BEFORE the first await. The confirm gate below is
    // asynchronous, and claiming after it let two clicks sail through the
    // duplicate check together and place the order twice.
    if (this._sentTokens.has(token)) {
      const held = this._orders.get(token);
      return {
        ok: false,
        reason: held?.intent === 'AMBIGUOUS'
          ? 'duplicate clientToken: the first attempt was never confirmed and may be live'
          : 'duplicate clientToken (idempotent skip)',
        clientId: token,
        state: held?.state,
        intent: held?.intent,
      };
    }
    this._sentTokens.add(token);

    if (!this._armed) {
      let approved = false;
      try {
        approved = await (this._gate ? this._gate(req) : Promise.resolve(false));
      } catch (err) {
        // The gate runs before any network call, so nothing can be live.
        this._sentTokens.delete(token);
        throw err;
      }
      if (!approved) {
        // Declining is a pre-flight outcome: the request provably never left, so
        // the token is free and the same one may be offered again.
        this._sentTokens.delete(token);
        return { ok: false, reason: 'not confirmed', intent: 'BLOCKED' };
      }
    }

    const finalReq: PlaceRequest = { ...req, price: snappedPrice, triggerPrice: snappedTrigger, clientToken: token };
    const tracked: Tracked = { clientId: token, state: 'pending_place', intent: 'SUBMITTING', req: finalReq };
    this._orders.set(token, tracked);

    try {
      const { orderId } = await this._feed.place({ ...finalReq, mode: this._mode });
      tracked.brokerId = orderId;
      this._byBroker.set(orderId, token);
      // `state` keeps its historical optimism for existing consumers. `intent`
      // stops at SUBMITTED and `brokerStatus` stays undefined, because the
      // transport answering is not the exchange answering.
      tracked.state = transition(tracked.state, 'ack');
      tracked.intent = 'SUBMITTED';
      return { ok: true, clientId: token, state: tracked.state, intent: tracked.intent };
    } catch (err) {
      const preflight = isPreflightFailure(err);
      tracked.state = transition(tracked.state, 'reject');
      tracked.intent = preflight ? 'BLOCKED' : 'AMBIGUOUS';
      // A transport failure says the RESPONSE did not arrive. It says nothing
      // about whether the REQUEST did: a 504, a socket reset after the body was
      // flushed, a tab suspended mid-flight all leave the order possibly live.
      // Releasing the token would make the retry indistinguishable from a first
      // attempt to every layer that dedupes on it, and double a live position on
      // a chart that draws Buy and Sell buttons. So the token is kept unless the
      // feed proves the request never left. Pessimistic is the safe default.
      if (preflight) this._sentTokens.delete(token);
      const message = String((err as Error).message ?? err);
      this._settle(tracked);
      return {
        ok: false,
        clientId: token,
        state: tracked.state,
        intent: tracked.intent,
        reason: preflight ? message : `${message} (may have reached the broker; check the order book before retrying)`,
      };
    }
  }

  /** One-click market order. Omitting `opts` is exactly the previous behaviour. */
  public placeMarket(symbol: string, side: OrderSide, qty: number, opts?: MarketOrderOptions): Promise<PlaceResult> {
    return this.placeOrder({
      symbol,
      side,
      type: 'MARKET',
      qty,
      exchange: opts?.exchange,
      product: opts?.product,
      clientToken: opts?.clientToken,
    });
  }

  /** Link two orders as OCO: when one fills/cancels, the other is cancelled. */
  public linkOco(clientIdA: string, clientIdB: string): void {
    const a = this._orders.get(clientIdA);
    const b = this._orders.get(clientIdB);
    if (a && b) { a.ocoPeer = clientIdB; b.ocoPeer = clientIdA; }
  }

  /**
   * Rate-limited modify (drag): coalesces to the latest, sends at most every
   * minModifyMs. An invalid price (tick/band/freeze) is NOT enqueued or sent:
   * it surfaces via `onValidationError` so the UI can snap the line back.
   */
  public requestModify(clientId: string, price: number, opts?: ModifyOptions): void {
    const o = this._orders.get(clientId);
    if (o === undefined || isTerminal(o.state)) return;
    const built = this._buildModifyPatch(o, price, opts?.triggerPrice);
    if (!built.ok) {
      this._onValidationError?.(built.reason);
      return; // do NOT send an out-of-band modify to the broker
    }
    this._pendingModify.set(clientId, built.patch);
    const last = this._lastModifyAt.get(clientId) ?? -Infinity;
    if (this._now() - last >= this._minModifyMs) void this._flushModify(clientId);
  }

  /** Force-send any pending modify (e.g. on drag end). */
  public commitModify(clientId: string): Promise<void> {
    return this._flushModify(clientId);
  }

  /**
   * Turn a dragged level into a patch the broker can apply without losing a
   * field it already holds.
   */
  private _buildModifyPatch(o: Tracked, price: number, explicitTrigger?: number): PatchResult {
    const v = validateOrder(price, o.req.qty, this._constraints);
    if (!v.ok) return { ok: false, reason: v.reason ?? 'invalid modify price' };
    const level = v.price ?? price;

    if (o.req.type === 'SL-M') {
      // Stop-market has a trigger and no limit price at all. The dragged level
      // IS the trigger; writing it to `price` dropped the stop and handed the
      // broker a limit the order never had.
      const trigger = explicitTrigger ?? level;
      const t = this._validateTrigger(trigger, o.req.qty);
      return t.ok ? { ok: true, patch: { triggerPrice: t.price } } : t;
    }

    if (o.req.type === 'SL') {
      // Stop-limit moves as a pair. The gap between limit and trigger is the
      // user's decision, so carry it rather than collapsing the two onto one
      // level or, as before, leaving the trigger behind at the old price.
      const offset = o.req.triggerPrice !== undefined && o.req.price !== undefined
        ? o.req.triggerPrice - o.req.price
        : 0;
      const trigger = explicitTrigger ?? level + offset;
      const t = this._validateTrigger(trigger, o.req.qty);
      return t.ok ? { ok: true, patch: { price: level, triggerPrice: t.price } } : t;
    }

    if (explicitTrigger !== undefined) {
      const t = this._validateTrigger(explicitTrigger, o.req.qty);
      return t.ok ? { ok: true, patch: { price: level, triggerPrice: t.price } } : t;
    }
    return { ok: true, patch: { price: level } };
  }

  private _validateTrigger(trigger: number, qty: number): { ok: true; price: number } | { ok: false; reason: string } {
    const t = validateOrder(trigger, qty, this._constraints);
    if (!t.ok) return { ok: false, reason: `trigger: ${t.reason ?? 'invalid'}` };
    return { ok: true, price: t.price ?? trigger };
  }

  private async _flushModify(clientId: string): Promise<void> {
    const patch = this._pendingModify.get(clientId);
    const o = this._orders.get(clientId);
    if (patch === undefined || o === undefined || o.brokerId === undefined) return;
    this._pendingModify.delete(clientId);
    this._lastModifyAt.set(clientId, this._now());
    o.state = transition(o.state, 'submitModify');
    o.intent = 'MODIFY_SUBMITTING';
    try {
      await this._feed.modify(o.brokerId, patch);
      o.state = transition(o.state, 'ack');
      o.intent = 'ACKNOWLEDGED';
      // Track what we asked for, so the next drag derives its stop offset from
      // the level the order is now resting at.
      if (patch.price !== undefined) o.req = { ...o.req, price: patch.price };
      if (patch.triggerPrice !== undefined) o.req = { ...o.req, triggerPrice: patch.triggerPrice };
    } catch (err) {
      o.state = transition(o.state, 'reject');
      // A failed modify may still have been applied; only a pre-flight failure
      // rules that out and leaves the order where we last knew it to be.
      o.intent = isPreflightFailure(err) ? 'ACKNOWLEDGED' : 'AMBIGUOUS';
    }
  }

  public async cancelOrder(clientId: string): Promise<void> {
    const o = this._orders.get(clientId);
    if (o === undefined || o.brokerId === undefined || isTerminal(o.state)) return;
    o.state = transition(o.state, 'submitCancel');
    o.intent = 'CANCEL_SUBMITTING';
    try {
      await this._feed.cancel(o.brokerId);
      o.state = transition(o.state, 'cancelled');
      // Transport-level only. The order line may stop drawing, but the broker
      // has not said the order is gone, so `brokerStatus` stays untouched.
      o.intent = 'ACKNOWLEDGED';
      this._cancelOcoPeer(o);
      this._settle(o);
    } catch (err) {
      o.state = transition(o.state, 'reject');
      o.intent = isPreflightFailure(err) ? 'ACKNOWLEDGED' : 'AMBIGUOUS';
    }
  }

  /** Broker fill event (by broker id). Advances state and triggers OCO. */
  public onFill(brokerId: string, full: boolean): void {
    this.onBrokerUpdate(brokerId, full ? 'filled' : 'partial');
  }

  /**
   * Authoritative state from the broker: an order-stream push or a poll of the
   * book. This is the ONLY input that writes `brokerStatus`; place, modify and
   * cancel resolving never do.
   */
  public onBrokerUpdate(brokerId: string, status: OrderStatus): void {
    const clientId = this._byBroker.get(brokerId);
    if (clientId === undefined) return;
    const o = this._orders.get(clientId);
    if (o === undefined) return;
    o.brokerStatus = status;
    const event = BROKER_EVENT[status];
    if (event !== undefined) o.state = transition(o.state, event);
    o.intent = BROKER_FINAL.has(status) ? 'SETTLED' : 'ACKNOWLEDGED';
    if (status === 'filled') this._cancelOcoPeer(o);
    this._settle(o);
  }

  private _cancelOcoPeer(o: Tracked): void {
    if (o.ocoPeer === undefined) return;
    const peer = this._orders.get(o.ocoPeer);
    if (peer && !isTerminal(peer.state)) void this.cancelOrder(peer.clientId);
  }

  /**
   * Enter reconciliation: the connection dropped or a gap was seen, so our
   * picture may be behind. Nothing is concluded here; call `onReconnect` with
   * the fresh book to conclude anything. An AMBIGUOUS row is left alone, since
   * only the broker can take it out of that state.
   */
  public beginReconcile(): void {
    for (const o of this._orders.values()) {
      if (isTerminal(o.state) || o.intent === 'AMBIGUOUS') continue;
      o.intent = 'RECONCILING';
    }
  }

  /**
   * Apply a fresh order-book snapshot. Absence is not death: `state` still goes
   * to `stale` for existing consumers, but the intent becomes AMBIGUOUS, because
   * an order missing from one snapshot means our picture is incomplete, not that
   * the broker cancelled it. Such a row is never pruned. (v2 drops `stale`
   * entirely; that removal has to wait for the contract bump.)
   */
  public onReconnect(presentBrokerIds: ReadonlySet<string>): void {
    for (const o of this._orders.values()) {
      if (isTerminal(o.state)) continue;
      if (o.brokerId !== undefined && presentBrokerIds.has(o.brokerId)) {
        o.intent = 'ACKNOWLEDGED';
        continue;
      }
      o.state = transition(o.state, 'reconnectAbsent');
      o.intent = 'AMBIGUOUS';
    }
  }

  /**
   * Terminal bookkeeping. Per-order scratch (throttle stamps, coalesced patches)
   * goes at once; the row itself survives so a host can still read the final
   * state, and only the oldest are evicted once `maxSettledOrders` is passed.
   *
   * Idempotency tokens are never pruned. They are the record of what this client
   * has put on the wire, and dropping one so a map stays small is the same
   * mistake as releasing it on a transport error.
   */
  private _settle(o: Tracked): void {
    if (o.pruned === true || !isTerminal(o.state)) return;
    // An ambiguous or reconciling row is not over, whatever `state` says.
    if (o.intent === 'AMBIGUOUS' || o.intent === 'RECONCILING') return;
    o.pruned = true;
    this._lastModifyAt.delete(o.clientId);
    this._pendingModify.delete(o.clientId);
    this._settledIds.push(o.clientId);
    while (this._settledIds.length > this._maxSettled) {
      const id = this._settledIds.shift();
      if (id === undefined) break;
      const row = this._orders.get(id);
      // A token released by a pre-flight failure can be offered again, so the id
      // may now hold a LIVE row. Evicting that would drop a working order from
      // the book and with it the broker-id lookup that routes its fills.
      if (row === undefined || row.pruned !== true) continue;
      this._orders.delete(id);
      if (row.brokerId !== undefined) this._byBroker.delete(row.brokerId);
      this._lastModifyAt.delete(id);
      this._pendingModify.delete(id);
    }
  }
}
