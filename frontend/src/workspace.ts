import { stripView, type Widget, type WidgetChartState } from 'openalgo-charts/widget';
import { API_BASE, probeDatalakeAnchor } from './feed';

/**
 * Chart-state persistence — **one store**.
 *
 * The server workspace blob is the only place a layout is kept. The widget's own
 * persistence (`persist: true`, i.e. `localStorage`) is deliberately unused, so
 * a layout can never be restored from two sources that disagree.
 *
 * That used to be exactly what happened. The widget restored from `localStorage`
 * inside `createWidget` — including its `symbol`/`interval` — and the host then
 * applied the server blob over it, with a documented "the server wins" rule. Two
 * stores meant:
 *
 *  - which layout you landed on was decided by `localStorage` (it supplied the
 *    symbol/interval the layout id is derived from), while the *contents* came
 *    from the server, so the two could not be reconciled;
 *  - `localStorage` carried no series fingerprint, so the one class of bug the
 *    server blob was hardened against was still live on the other path.
 *
 * Everything the widget's store held (`symbol`, `exchange`, `interval`,
 * `chartType`, `theme`, `chart`, `rail`) is already inside `getState()`, and the
 * host never used `context.storage` for anything of its own, so nothing is lost.
 *
 * ## Restore runs before the widget exists
 *
 * The layout is loaded and *validated* before `createWidget`, then applied with
 * `restoreState`. Two reasons:
 *
 *  1. There is no second source to wait for, so nothing has to be reconciled
 *     asynchronously — the old code raced its restore against the first history
 *     load and needed a "wait for bars to settle" step to break the tie.
 *  2. `restoreState` only reloads when the instrument actually changes, so
 *     booting the widget *with* the stored instrument keeps it to one load
 *     instead of a flash of the default symbol plus a second fetch.
 */

const SAVE_DEBOUNCE_MS = 2000;

/** How long to wait for the first bar load before deciding the view. */
const DATA_WAIT_MS = 5000;

/**
 * Blob format this host writes. Bumped whenever the stored shape changes, so a
 * blob from an older build is recognised as unvalidatable instead of being read
 * as if it carried a fingerprint.
 */
const BLOB_VERSION = 2;

/** What we store: the engine state plus the identity of the series it captured. */
interface StoredBlob {
  v: number;
  /** Last bar time of the captured series; null when it had no bars. */
  series: number | null;
  state: unknown;
}

/** One row of `GET /api/charts/workspace` (metadata only, no blob). */
interface WorkspaceMeta {
  layout_id?: unknown;
  revision?: unknown;
  updated_at?: unknown;
}

export interface BootInstrument {
  symbol: string;
  exchange: string;
  interval: string;
}

export interface BootOptions extends BootInstrument {
  /** Intervals this host serves; a stored interval outside the set is ignored. */
  intervals: readonly string[];
}

export interface BootWorkspace extends BootInstrument {
  /** Layout id the restored state was stored under. */
  layoutId: string;
  /** Validated widget state to hand `restoreState`, or null when nothing was stored. */
  state: unknown | null;
  /** The series the returned view belongs to, per the datalake probe. */
  series: number | null;
  /** False when the stored view was dropped — stale, unvalidatable, or absent. */
  viewTrusted: boolean;
  /** Known revisions per layout id, for optimistic concurrency. */
  revisions: Record<string, number>;
}

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);

/** Layout id for an instrument — the same derivation the save path uses. */
const layoutIdOf = (i: BootInstrument): string =>
  `${i.exchange}_${i.symbol}_${i.interval}_default`;

/**
 * Last bar time of the chart's series — the dataset's identity.
 *
 * A viewport is a range of *logical bar indices*, so it only means anything
 * against the series it was captured on. Symbol + exchange + interval does not
 * identify that series: the backend's coverage for one (symbol, interval) grows
 * whenever the datalake syncs, so a viewport captured over zero bars restored
 * over nine hundred as a chart scrolled to a meaningless place. The last bar
 * time pins the actual series, and it survives the one change that is *not* a
 * new dataset — older bars paged in at the front.
 */
const lastBarTime = (widget: Widget): number | null => {
  const bars = widget.series.getData();
  for (let i = bars.length - 1; i >= 0; i -= 1) {
    const bar = bars[i];
    if (bar !== undefined && typeof bar.time === 'number') return bar.time;
  }
  return null;
};

/**
 * Resolve with the series identity once bars are present, or when the wait
 * expires.
 *
 * Only the save path needs this. A save that runs before the first load would
 * record `series: null`, and the next boot would then strip a perfectly good
 * view — the safe direction, but needlessly lossy. The restore path needs no
 * wait: it validates against the datalake probe, before the widget exists.
 */
const settledSeries = (widget: Widget): Promise<number | null> =>
  new Promise((resolve) => {
    const present = lastBarTime(widget);
    if (present !== null) {
      resolve(present);
      return;
    }
    let off: () => void = () => undefined;
    const timer = setTimeout(() => {
      off();
      resolve(lastBarTime(widget));
    }, DATA_WAIT_MS);
    off = widget.on('data', () => {
      if (lastBarTime(widget) === null) return;
      clearTimeout(timer);
      off();
      resolve(lastBarTime(widget));
    });
  });

/**
 * The **active** workspace: the most recently saved layout.
 *
 * There is no separate pointer to keep in step, and that is deliberate. A layout
 * id is derived from its own instrument, so the last save *is* the workspace the
 * user was in — the rule is a property of the data rather than a second record
 * that can drift from it. (A pointer would be one more thing to keep
 * consistent, which is the failure mode this module exists to remove.)
 */
const newestLayout = (metas: unknown): WorkspaceMeta | null => {
  if (!Array.isArray(metas)) return null;
  let best: WorkspaceMeta | null = null;
  let bestAt = -Infinity;
  for (const row of metas) {
    if (!isRecord(row)) continue;
    const id = row.layout_id;
    if (typeof id !== 'string' || id === '') continue;
    // Parse rather than compare strings: ISO-8601 stays lexicographic only
    // while every row shares one offset.
    const at = typeof row.updated_at === 'string' ? Date.parse(row.updated_at) : NaN;
    const stamp = Number.isNaN(at) ? 0 : at;
    if (best === null || stamp > bestAt) {
      best = row as WorkspaceMeta;
      bestAt = stamp;
    }
  }
  return best;
};

/** Keep the layout, drop the view — the engine's own rule for newer data. */
const stripViewOf = (state: unknown): unknown =>
  isRecord(state) && isRecord(state.chart)
    ? { ...state, chart: stripView(state.chart as unknown as WidgetChartState) }
    : state;

const readRevisions = (metas: unknown): Record<string, number> => {
  const out: Record<string, number> = {};
  if (!Array.isArray(metas)) return out;
  for (const row of metas) {
    if (!isRecord(row)) continue;
    const id = row.layout_id;
    const rev = row.revision;
    if (typeof id === 'string' && typeof rev === 'number') out[id] = rev;
  }
  return out;
};

/**
 * Load and validate the active workspace, before the widget is built.
 *
 * The fingerprint check compares the stored series against a datalake probe for
 * the *stored* instrument, so it is exact rather than "whatever the widget
 * happens to have loaded so far". The probe is the same request that seeds the
 * load-window clock, so validating costs no extra round trip.
 *
 * Best-effort by design: any failure (no session, no server, malformed blob)
 * boots the host defaults rather than blocking the chart.
 */
export async function loadActiveWorkspace(options: BootOptions): Promise<BootWorkspace> {
  const fallback: BootInstrument = {
    symbol: options.symbol.trim().toUpperCase(),
    exchange: options.exchange.trim().toUpperCase(),
    interval: options.interval,
  };
  const empty: BootWorkspace = {
    ...fallback,
    layoutId: layoutIdOf(fallback),
    state: null,
    series: null,
    viewTrusted: false,
    revisions: {},
  };

  let envelope: StoredBlob | null = null;
  let state: unknown = null;
  let revisions: Record<string, number> = {};
  let storedInstrument: BootInstrument | null = null;

  try {
    const list = await fetch(`${API_BASE}/api/charts/workspace`);
    if (list.ok) {
      const metas: unknown = await list.json();
      revisions = readRevisions(metas);
      const active = newestLayout(metas);
      if (active !== null && typeof active.layout_id === 'string') {
        const id = active.layout_id;
        const res = await fetch(`${API_BASE}/api/charts/workspace/${encodeURIComponent(id)}`);
        if (res.ok) {
          const payload = (await res.json()) as { data?: unknown };
          envelope = payload.data as StoredBlob | null;
          state = envelope?.state ?? null;
          storedInstrument = { symbol: options.symbol, exchange: options.exchange, interval: options.interval };
        }
      }
    }
  } catch (err) {
    console.warn('workspace load failed', err);
  }

  // Boot into the stored instrument so `restoreState` sees an unchanged
  // instrument: it then keeps the view and does not reload.
  const instrument = storedInstrument ?? fallback;
  const layoutId = layoutIdOf(instrument);

  // This also seeds the load-window clock for the instrument we are about to
  // open, so the probe must happen before `createWidget`.
  const series = await probeDatalakeAnchor(instrument.exchange, instrument.symbol, instrument.interval);

  // A view is trustworthy only against the series it was captured on. `null` is
  // never a match: a blob saved over an empty series has no view worth keeping.
  const viewTrusted =
    envelope !== null &&
    envelope.v === BLOB_VERSION &&
    series !== null &&
    envelope.series === series;

  return {
    ...instrument,
    layoutId,
    state: state === null ? null : viewTrusted ? state : stripViewOf(state),
    series,
    viewTrusted,
    revisions,
  };
}

/**
 * Chart-state persistence over the workspace CRUD API — the only store.
 *
 * Save-only: the restore already happened in `loadActiveWorkspace`, so this
 * binds the writes. The layout id follows the live instrument, so switching
 * symbol or interval starts saving under that instrument's own layout.
 */
export function mountWorkspace(widget: Widget, boot: BootWorkspace): void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  // Known revision per layout id. Seeded from the boot list and updated on every
  // successful write; a layout never seen before writes with `null`, which the
  // server treats as "no precondition" and creates.
  const revisions: Record<string, number> = { ...boot.revisions };
  // False until the boot restore's outcome is known. A save that fires earlier
  // would PUT with a `null` revision and clobber the stored layout the restore
  // just handed back.
  let ready = false;
  // Set when a change arrives while boot is still settling; one flush is
  // re-scheduled once the outcome is known.
  let dirtyWhileRestoring = false;

  const layoutId = (): string =>
    `${widget.exchange()}_${widget.symbol()}_${widget.interval()}_default`;

  /** The blob we write: engine state plus the series it was captured on. */
  const payload = (id: string): { data: StoredBlob; revision: number | null; force: boolean } => ({
    data: { v: BLOB_VERSION, series: lastBarTime(widget), state: widget.getState() },
    revision: revisions[id] ?? null,
    force: false,
  });

  const put = async (init: RequestInit = {}): Promise<void> => {
    const id = layoutId();
    const url = `${API_BASE}/api/charts/workspace/${encodeURIComponent(id)}`;
    const send = async (body: unknown): Promise<Response> =>
      fetch(url, {
        ...init,
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      });

    let res = await send(payload(id));
    if (res.status === 409) {
      // Another client wrote this layout first. With one store there is nothing
      // to reconcile: read the current revision and re-put our own state, so the
      // conflict costs a retry rather than yanking the user's viewport out from
      // under them mid-session.
      const stored = await fetch(url);
      if (stored.ok) {
        const blob = (await stored.json()) as { revision?: unknown };
        if (typeof blob.revision === 'number') revisions[id] = blob.revision;
      }
      res = await send(payload(id));
    }
    if (res.ok) {
      const done = (await res.json()) as { revision?: unknown };
      if (typeof done.revision === 'number') revisions[id] = done.revision;
    }
  };

  const save = (): void => {
    if (!ready) {
      dirtyWhileRestoring = true; // boot restore still settling
      return;
    }
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      void put().catch((err: unknown) => console.warn('workspace save failed', err));
    }, SAVE_DEBOUNCE_MS);
  };

  // App-lifetime wiring: the widget owns the page, so nothing unsubscribes.
  widget.on('layout', () => save());
  widget.on('symbol', () => save());
  widget.on('interval', () => save());
  // Drawings reach `save()` only *incidentally*: the widget's `layout` event is
  // wired to `objects:change`, and a drawing edit is not itself in that list —
  // the library's own handler for `draw:*` writes straight to its store, which
  // this host no longer provides. Today a drawn shape does land in the blob (the
  // draw tier feeds the objects inventory, which emits `objects:change`), but
  // that is one refactor away from silently dropping every drawing. Subscribe to
  // the draw events directly so persistence does not depend on that coupling.
  for (const ev of ['draw:add', 'draw:remove', 'draw:update', 'draw:paste', 'draw:cut']) {
    widget.chart.on(ev, () => save());
  }
  // A debounce pending at tab close still holds the user's last 2s of work;
  // keepalive lets the request outlive the page.
  window.addEventListener('pagehide', () => {
    if (!ready || timer === null) return;
    clearTimeout(timer);
    timer = null;
    void put({ keepalive: true }).catch(() => undefined);
  });

  // The widget emits `layout` only for *structural* changes (panes, indicators,
  // axis, drawings) — nothing fires on a plain load. So a layout whose view we
  // could not keep leaves no blob at all, and its next load has no fingerprint
  // to compare against. Wait for the bars that decide the series, then record one.
  void settledSeries(widget).then(() => {
    ready = true;
    if (dirtyWhileRestoring) {
      dirtyWhileRestoring = false;
      save();
    } else if (!boot.viewTrusted) {
      save();
    }
  });
}
