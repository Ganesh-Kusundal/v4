import {
  runTransform,
  HeikinAshiTransform,
  RenkoTransform,
  RangeBarsTransform,
  LineBreakTransform,
  PointFigureTransform,
  KagiTransform,
} from 'openalgo-charts/transform';

export {
  runTransform,
  HeikinAshiTransform,
  RenkoTransform,
  RangeBarsTransform,
  LineBreakTransform,
  PointFigureTransform,
  KagiTransform,
};

/** Engine token -> backend interval param (`1m|5m|15m|30m|1h|D`). */
export const mapInterval = (interval: string): string =>
  interval === 'D' || interval === '1d' ? 'D' : interval;

/** Engine token -> WS interval param. Backend Timeframe values are 1m|5m|15m|30m|1h|1d. */
export const mapWsInterval = (interval: string): string => (interval === 'D' ? '1d' : interval);
