import type { BarsRequest, DataFeed, MarketDepth } from 'openalgo-charts';
import { DomLadder } from 'openalgo-charts/trade';
import type { Widget } from 'openalgo-charts/widget';

/**
 * Mount the DOM ladder as a chart primitive and feed it depth frames.
 * Remounts itself on symbol change (unsubscribes the old instrument, resubscribes
 * the new one); the returned function undoes everything.
 */
export function mountLadder(widget: Widget, feed: DataFeed, req: BarsRequest): () => void {
  const ladder = new DomLadder({ tickSize: 0.05 });
  widget.chart.addPrimitive(ladder);

  let unsubDepth: (() => void) | null = null;
  const subscribe = (r: BarsRequest): void => {
    unsubDepth?.();
    unsubDepth = feed.subscribeDepth?.(r, (depth: MarketDepth) => ladder.setDepth(depth)) ?? null;
  };
  subscribe(req);

  const offSymbol = widget.on('symbol', ({ symbol, exchange }) => {
    subscribe({ symbol, exchange, interval: req.interval });
  });

  return () => {
    offSymbol();
    unsubDepth?.();
    widget.chart.removePrimitive(ladder);
  };
}
