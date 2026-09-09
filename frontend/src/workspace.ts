import type { Widget } from 'openalgo-charts/widget';
import { API_BASE } from './feed';

const SAVE_DEBOUNCE_MS = 2000;

/**
 * Chart-state persistence over the workspace CRUD API, alongside the widget's
 * own localStorage persistence (`persist: true`). Precedence: localStorage
 * restores inside createWidget and keeps UI chrome (theme, rail); the server
 * blob restores right after it and carries the chart state, so the server
 * wins wherever the two disagree.
 */
export function mountWorkspace(widget: Widget): void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  // Last revision seen for the layout we last wrote or read; null until the
  // first GET/PUT answers. The backend uses it for optimistic concurrency.
  let revision: number | null = null;
  // False until the startup restore's outcome is known. A save that fires
  // earlier would PUT with `revision: null` and clobber the stored layout the
  // restore is about to hand back.
  let ready = false;

  const layoutId = (): string => `${widget.exchange()}_${widget.symbol()}_${widget.interval()}_default`;

  const put = async (init: RequestInit = {}): Promise<void> => {
    const id = layoutId();
    const url = `${API_BASE}/api/charts/workspace/${id}`;
    const res = await fetch(url, {
      ...init,
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ data: widget.getState(), revision, force: false }),
    });
    if (res.status === 409) {
      // Stale revision: take the stored blob, re-apply it, retry once fresh.
      const stored = await fetch(url);
      if (!stored.ok) return;
      const blob = (await stored.json()) as { data?: unknown; revision?: unknown };
      revision = typeof blob.revision === 'number' ? blob.revision : null;
      widget.restoreState(blob.data);
      const retry = await fetch(url, {
        ...init,
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ data: widget.getState(), revision, force: false }),
      });
      if (retry.ok) {
        const done = (await retry.json()) as { revision?: unknown };
        revision = typeof done.revision === 'number' ? done.revision : revision;
      }
      return;
    }
    if (res.ok) {
      const done = (await res.json()) as { revision?: unknown };
      revision = typeof done.revision === 'number' ? done.revision : revision;
    }
  };

  const save = (): void => {
    if (!ready) return; // startup restore still in flight
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
  // A debounce pending at tab close still holds the user's last 2s of work;
  // keepalive lets the request outlive the page.
  window.addEventListener('pagehide', () => {
    if (timer === null) return;
    clearTimeout(timer);
    timer = null;
    void put({ keepalive: true }).catch(() => undefined);
  });

  // Startup restore, after the widget's own localStorage pass. The list
  // endpoint 200s with metadata even for a never-saved layout (a direct GET
  // would 404 and Chrome logs every failed resource as a console error), so
  // probe there first and only then fetch the blob.
  void (async () => {
    try {
      const list = await fetch(`${API_BASE}/api/charts/workspace`);
      if (!list.ok) return;
      const metas = (await list.json()) as Array<{ layout_id?: unknown; revision?: unknown }>;
      const id = layoutId();
      const meta = metas.find((m) => m.layout_id === id);
      if (meta === undefined) return; // never saved
      revision = typeof meta.revision === 'number' ? meta.revision : null;
      const res = await fetch(`${API_BASE}/api/charts/workspace/${id}`);
      if (!res.ok) return;
      const blob = (await res.json()) as { data?: unknown };
      widget.restoreState(blob.data);
    } catch (err) {
      console.warn('workspace restore failed', err);
    } finally {
      // The restore's outcome (and so `revision`) is now known either way;
      // saves from here carry the right concurrency token.
      ready = true;
    }
  })();
}
