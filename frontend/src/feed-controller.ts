// Minimum socket surface FeedController consumes.
// ponytail: structural type — avoids importing the unexported BarSocket class.
export interface FeedSocket {
  on(type: string, handler: (msg: unknown) => void): () => void;
  onClose(cb: () => void): () => void;
  onGiveUp(cb: () => void): () => void;
}

export interface FeedStatusEvent {
  /** Server-pushed supervisor state label; null when no supervisor is active. */
  feedState: string | null;
  /**
   * True when the server has explicitly blocked readiness (ready===false) or
   * the WebSocket connection is gone. The chart data controller's own readiness
   * (bars loaded) is a separate gate — FeedController does not own it.
   */
  serverBlocked: boolean;
  /** Status-region message to surface; absent when no message change is needed. */
  message?: { text: string; kind: 'error' };
}

export type FeedStatusHandler = (event: FeedStatusEvent) => void;

/**
 * Thin seam that owns the server-driven side of the feed-readiness gate.
 *
 * Responsibilities (exclusively):
 * - subscribe to `feed_status` frames from the WebSocket and translate them;
 * - clear server-block when the supervisor reports ready=true;
 * - set server-block on socket close / give-up.
 *
 * Not responsible for:
 * - chart data controller readiness (bars loaded) — main.ts / applyDataState;
 * - order intent validation — order-safety.ts;
 * - replay state — replay.ts / ReplayGuard (B6).
 *
 * ponytail: one seam, no mass-move. Ceiling: single-client; multi-client
 * scoped leases are a follow-up ownership concern.
 */
export class FeedController {
  private _serverBlocked = false;
  private readonly _unsubs: Array<() => void> = [];

  constructor(socket: FeedSocket, onStatus: FeedStatusHandler) {
    this._unsubs.push(
      socket.on('feed_status', (msg) => {
        const frame = msg as { ready?: boolean | null; state?: string | null };
        if (frame.ready === false) {
          this._serverBlocked = true;
        } else if (frame.ready === true) {
          // Supervisor recovered — clear the server block so the chart data
          // controller can re-assert feedReady on its next tick.
          this._serverBlocked = false;
        }
        // null/undefined ready: no supervisor (paper/backtest) — leave block as-is.
        onStatus({ feedState: frame.state ?? null, serverBlocked: this._serverBlocked });
      }),
      socket.onClose(() => {
        this._serverBlocked = true;
        onStatus({
          feedState: null,
          serverBlocked: true,
          message: { text: 'Order entry disabled: feed reconnecting', kind: 'error' },
        });
      }),
      socket.onGiveUp(() => {
        this._serverBlocked = true;
        onStatus({
          feedState: null,
          serverBlocked: true,
          message: { text: 'Order entry disabled: feed unavailable', kind: 'error' },
        });
      }),
    );
  }

  get serverBlocked(): boolean {
    return this._serverBlocked;
  }

  dispose(): void {
    for (const unsub of this._unsubs) unsub();
    this._unsubs.length = 0;
  }
}
